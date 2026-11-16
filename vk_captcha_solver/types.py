from typing import TypedDict, List, Optional, Union, Dict, Any, Literal


class IApiOptions(TypedDict, total=False):
    """Options of a CaptchaSolver; every key is optional."""

    #: API version of the old captcha page's calls (default 5.199).
    version: str
    #: API base of the old captcha page's calls (default https://api.vk.ru).
    baseUrl: str
    #: Added to every request; a User-Agent here is ignored (see userAgent).
    headers: Dict[str, str]
    #: Go with every request, besides the remixmdevice cookie.
    cookies: Dict[str, str]
    #: The User-Agent of every request; default the browser profile's, which
    #: also sends the profile's client hints.
    userAgent: str
    #: The proxy of a solving that names none (http, https, socks5, socks5h).
    proxy: str
    #: Seconds a whole solving may take (default 60).
    timeout: float
    #: Seconds one try at loading the captcha page may take (default 10; the
    #: page gets two tries).
    pageTimeout: float
    #: Seconds one request may take (default 30).
    requestTimeout: float
    #: IANA time zone the browser claims (default: VK_CAPTCHA_TIMEZONE, TZ
    #: when it names a zone, else Europe/Moscow).
    timezone: str


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
    captchaScriptUrl: str


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
    # Device / connection data
    debug_info: str
    connectionDownlink: List[float]
    connectionRtt: List[int]
    browser_fp: str


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


class IFakeBrowserEnvironment(TypedDict):
    screenWidth: int
    screenHeight: int
    screenAvailWidth: int
    screenAvailHeight: int
    innerWidth: int
    innerHeight: int
    devicePixelRatio: float
    language: str
    languages: List[str]
    webdriver: bool
    hardwareConcurrency: int
    deviceMemory: int
    connectionEffectiveType: str
    notificationsPermission: str
