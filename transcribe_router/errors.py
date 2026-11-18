"""统一的错误类型。

路由层只依赖 ``category`` 决定下一步动作：

- auth        鉴权失败 -> 该家临时下线，换下一家
- quota       额度耗尽 -> 该家标记为已用完，换下一家
- file        超出单文件上限 / 输入不可用 -> 换下一家
- unsupported 该家不支持这个输入（例如只收公网 URL）-> 换下一家
- transient   网络抖动、5xx、限流 -> 原地重试，重试完仍失败再换下一家
- fatal       参数错误等，换一家也没用，但仍按“降级”处理以保持任务可继续
"""


class ConfigError(Exception):
    """配置文件/参数错误，直接退出。"""


class AudioError(Exception):
    """ffmpeg / ffprobe 相关错误。"""


class JobBusy(Exception):
    """同一个音频文件正在被另一个进程处理。"""


class ProviderError(Exception):
    category = "fatal"

    def __init__(self, message, *, detail=None):
        super().__init__(message)
        self.detail = detail


class AuthError(ProviderError):
    category = "auth"


class QuotaExhausted(ProviderError):
    category = "quota"


class FileTooLarge(ProviderError):
    category = "file"


class Unsupported(ProviderError):
    category = "unsupported"


class TransientError(ProviderError):
    category = "transient"


class FatalProviderError(ProviderError):
    category = "fatal"


class TransportError(TransientError):
    """底层网络错误，归到 transient。"""


class AllProvidersFailed(ProviderError):
    category = "all_failed"
