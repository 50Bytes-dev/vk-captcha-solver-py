import io
import base64
import math
from typing import List, Tuple, Dict, Any
from PIL import Image
import numpy as np

from .types import ICaptchaContent, ITileInfo, ITile

class SliderCaptchaSolver:
    DEFAULT_TILE_COUNT = 5
    DEFAULT_MAX_STEPS = 50

    def solve(self, content: ICaptchaContent) -> Dict[str, Any]:
        base64_image = content.get('image')
        raw_steps = content.get('steps')

        if not base64_image or not isinstance(raw_steps, list) or len(raw_steps) == 0:
            return {'stepCount': 0, 'selectedSwaps': []}

        # Steps for processing: ignore first step as per TS code
        steps_for_processing = raw_steps[1:] if len(raw_steps) > 1 else []

        image_data = base64.b64decode(base64_image)
        original_image = Image.open(io.BytesIO(image_data)).convert('RGB')

        best_step, best_swaps = self.find_optimal_step_count(
            original_image,
            steps_for_processing,
            self.DEFAULT_MAX_STEPS,
            self.DEFAULT_TILE_COUNT,
            content['extension']
        )

        return {
            'stepCount': best_step,
            'selectedSwaps': best_swaps
        }

    def compute_tile_layout(self, image_width: int, image_height: int, tile_count: int) -> ITileInfo:
        vertical_lines = [
            round((i * image_width) / tile_count) for i in range(tile_count + 1)
        ]
        horizontal_lines = [
            round((i * image_height) / tile_count) for i in range(tile_count + 1)
        ]

        tiles: List[ITile] = []
        for row in range(tile_count):
            for col in range(tile_count):
                x = vertical_lines[col]
                y = horizontal_lines[row]
                width = vertical_lines[col + 1] - x
                height = horizontal_lines[row + 1] - y
                tiles.append({'x': x, 'y': y, 'width': width, 'height': height})

        return {
            'tiles': tiles,
            'grid': {
                'vertical': vertical_lines,
                'horizontal': horizontal_lines
            }
        }

    def calculate_seam_score(self, image: Image.Image, tile_count: int) -> int:
        # Convert to numpy array
        # shape: (height, width, 3)
        data = np.array(image, dtype=np.int16) # Use int16 to avoid overflow during subtraction
        height, width, _ = data.shape

        tile_info = self.compute_tile_layout(width, height, tile_count)
        grid = tile_info['grid']

        total_diff = 0

        # Vertical seams
        # Iterate over columns (seams are vertical lines between columns)
        # Seams are at indices 1 to tile_count-1
        for row in range(tile_count):
            y_start = grid['horizontal'][row]
            y_end = grid['horizontal'][row + 1]
            
            for col in range(1, tile_count):
                seam_x = grid['vertical'][col]
                
                # diff between pixel at seamX-1 and seamX
                # Slice: rows y_start:y_end, cols seam_x-1 and seam_x
                left_col = data[y_start:y_end, seam_x - 1, :]
                right_col = data[y_start:y_end, seam_x, :]
                
                diff = np.abs(left_col - right_col)
                total_diff += np.sum(diff)

        # Horizontal seams
        for col in range(tile_count):
            x_start = grid['vertical'][col]
            x_end = grid['vertical'][col + 1]
            
            for row in range(1, tile_count):
                seam_y = grid['horizontal'][row]
                
                # diff between pixel at seamY-1 and seamY
                top_row = data[seam_y - 1, x_start:x_end, :]
                bottom_row = data[seam_y, x_start:x_end, :]
                
                diff = np.abs(top_row - bottom_row)
                total_diff += np.sum(diff)

        return int(round(total_diff))

    def apply_tile_permutation(
        self,
        source_image: Image.Image,
        tile_layout: ITileInfo,
        permutation: List[int],
        output_width: int,
        output_height: int,
        extension: str
    ) -> Image.Image:
        
        new_image = Image.new('RGB', (output_width, output_height), (0, 0, 0))
        tile_count = int(math.sqrt(len(permutation)))
        tile_index = 0

        for row in range(tile_count):
            for col in range(tile_count):
                dest_x = tile_layout['grid']['vertical'][col]
                dest_y = tile_layout['grid']['horizontal'][row]
                dest_w = tile_layout['grid']['vertical'][col + 1] - dest_x
                dest_h = tile_layout['grid']['horizontal'][row + 1] - dest_y
                
                src_tile_idx = permutation[tile_index]
                src_tile = tile_layout['tiles'][src_tile_idx]
                
                # Extract
                tile_crop = source_image.crop((
                    src_tile['x'],
                    src_tile['y'],
                    src_tile['x'] + src_tile['width'],
                    src_tile['y'] + src_tile['height']
                ))
                
                # Resize (TS uses 'nearest' kernel)
                if tile_crop.size != (dest_w, dest_h):
                    tile_crop = tile_crop.resize((dest_w, dest_h), resample=Image.NEAREST)
                
                # Paste
                new_image.paste(tile_crop, (dest_x, dest_y))
                
                tile_index += 1

        return new_image

    def find_optimal_step_count(
        self,
        original_image: Image.Image,
        swap_sequence: List[int],
        max_steps: int,
        tile_count: int,
        extension: str
    ) -> Tuple[int, List[int]]:
        
        width, height = original_image.size
        tile_layout = self.compute_tile_layout(width, height, tile_count)

        current_permutation = list(range(tile_count * tile_count))
        best_score = float('inf')
        best_step = 0
        best_swaps: List[int] = []

        # Create a persistent copy? No, we just re-permute from original based on current permutation
        # Actually TS creates `originalBuffer` once and reuses it.
        # `applyTilePermutation` takes `sourceImage` and permutes it.
        
        for step in range(max_steps):
            swap_index = step * 2
            if swap_index + 1 >= len(swap_sequence):
                break

            a = swap_sequence[swap_index]
            b = swap_sequence[swap_index + 1]

            if 0 <= a < len(current_permutation) and 0 <= b < len(current_permutation):
                current_permutation[a], current_permutation[b] = current_permutation[b], current_permutation[a]
            
            permuted_image = self.apply_tile_permutation(
                original_image,
                tile_layout,
                current_permutation,
                width,
                height,
                extension
            )

            score = self.calculate_seam_score(permuted_image, tile_count)

            if score < best_score:
                best_score = score
                best_step = step + 1
                best_swaps = swap_sequence[:(step + 1) * 2]

        return best_step, best_swaps

