VERSION = "l0w/0.1"
SUPPORTED_VERSIONS = (VERSION,)
SIG_PREFIX = "l0wagent-sig/1"
MAX_SKEW = 300  # seconds

# messages.md
MAX_ENVELOPE = 16 * 1024 * 1024  # bytes, the whole envelope document
MAX_FILES = 5 * 1024 * 1024  # bytes, files in one message together
MAX_FRIEND_REQUEST_BODY = 4 * 1024  # bytes, serialized body of a tokenless friend request
ENVELOPE_MAX_AGE = 8 * 24 * 3600  # seconds an envelope's iat may lie in the past
