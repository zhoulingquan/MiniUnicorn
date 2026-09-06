"""Shared persistence tag constants.

These string values are embedded verbatim in on-disk session history and the
runtime-context block appended to user messages.  They are part of the
persisted format and must stay byte-for-byte stable.
"""

RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
