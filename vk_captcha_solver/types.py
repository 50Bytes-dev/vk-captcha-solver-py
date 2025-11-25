from typing import TypedDict, List, Optional, Union, Dict, Any, Literal


class IDelayOptions(TypedDict, total=False):
    """Delay options in seconds to avoid rate limiting."""

    between_requests: float  # Delay between API requests (default: 0.5)
    after_solve: float  # Delay after solving a captcha (default: 1.0)


class IApiOptions(TypedDict, total=False):
    version: str
    baseUrl: str
    headers: Dict[str, str]
    cookies: Dict[str, str]
    delays: IDelayOptions


class IApiError(TypedDict, total=False):
    error_code: int
    error_msg: str
    status: str
    request_params: List[Dict[str, Any]]


class IApiSuccessResponse(TypedDict):
    success: Literal[True]
    response: Any


class IApiErroredResponse(TypedDict):
    success: Literal[False]
    error: IApiError


class ICaptchaBaseResponse(TypedDict):
    status: Literal["OK", "ERROR"]


# This handles the union IApiResponse<T>
# Python doesn't enforce this strictly at runtime but helps static analysis
IApiResponse = Union[IApiSuccessResponse, IApiErroredResponse]


class ICaptchaSetting(TypedDict):
    type: str
    settings: str


class ICaptchaInitialData(TypedDict):
    variant: str
    domain: str
    session_token: str
    autofocus: str
    blank: str
    is_old_client: bool
    show_captcha_type: Literal["checkbox", "slider"]
    captcha_settings: List[ICaptchaSetting]
    platform: str
    captcha_id: str
    logo_enabled: bool


class ICaptchaInitialParamsResponse(TypedDict):
    data: ICaptchaInitialData


class ICaptchaInitialParams(ICaptchaInitialParamsResponse):
    difficulty: int
    powInput: str


class ICaptchaSettings(ICaptchaBaseResponse):
    sensors_delay: int
    bridge_sensors_list: List[
        Literal["accelerometer", "gyroscope", "motion", "cursor", "taps"]
    ]


class ICaptchaContent(ICaptchaBaseResponse):
    extension: Literal["jpeg", "png"]
    image: str
    steps: List[int]
    track: str


class ICaptchaSensorData(TypedDict):
    x: int
    y: int


class ICaptchaCheckParams(TypedDict, total=False):
    domain: str
    session_token: str
    hash: str
    answer: Optional[str]
    # Sensors
    accelerometer: List[ICaptchaSensorData]
    gyroscope: List[ICaptchaSensorData]
    motion: List[ICaptchaSensorData]
    cursor: List[ICaptchaSensorData]
    taps: List[ICaptchaSensorData]


class ICaptchaCheck(ICaptchaBaseResponse):
    redirect: str
    show_captcha_type: str
    success_token: str


class IMouseTraceParams(TypedDict, total=False):
    from_: Optional[ICaptchaSensorData]  # 'from' is a reserved keyword
    to: Optional[ICaptchaSensorData]
    intervalMs: Optional[int]
    durationMs: Optional[int]


class ITile(TypedDict):
    x: int
    y: int
    width: int
    height: int


class IGridLines(TypedDict):
    vertical: List[int]
    horizontal: List[int]


class ITileInfo(TypedDict):
    tiles: List[ITile]
    grid: IGridLines
