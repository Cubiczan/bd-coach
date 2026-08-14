"""Vendored Agora AccessToken2 / RtcTokenBuilder2.

Source: https://github.com/AgoraIO/Tools  (DynamicKey/AgoraDynamicKey/python3/src)
License: MIT, Copyright (c) 2023 Agora Community.

Vendored rather than pip-installed because the community PyPI package
(`agora-token-builder`) only emits the legacy 006 token format. These files are
the official AccessToken2 ("007") implementation and depend only on the stdlib.
Do not edit them; re-vendor from upstream instead.
"""

from .RtcTokenBuilder2 import RtcTokenBuilder, Role_Publisher, Role_Subscriber

__all__ = ["RtcTokenBuilder", "Role_Publisher", "Role_Subscriber"]
