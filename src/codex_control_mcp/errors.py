class BridgeError(Exception):
    def __init__(self, code, message, retryable=False, details=None):
        self.code, self.message, self.retryable, self.details = (
            code,
            message,
            retryable,
            details or {},
        )
        super().__init__(message)

    def as_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
