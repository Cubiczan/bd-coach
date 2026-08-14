"""Vendored Agora AccessToken2 / RtcTokenBuilder2.

Source: https://github.com/AgoraIO/Tools
  DynamicKey/AgoraDynamicKey/python3/src/{AccessToken2,RtcTokenBuilder2,Packer}.py
License: MIT, Copyright (c) 2023 Agora Community.

Vendored rather than pip-installed because the community PyPI package
(`agora-token-builder`) only emits the legacy 006 token format. These files are
the official AccessToken2 ("007") implementation and depend only on the stdlib.
Do not edit the vendored modules; re-vendor from upstream instead.

Local notes (not upstream patches):

- AccessToken.build() rebinds ``__app_id`` / ``__app_cert`` to utf-8 bytes. A
  second ``build()`` on the same instance would then fail ``__build_check``
  (32-char hex). ``RtcTokenBuilder`` always constructs a new ``AccessToken``
  per mint, so that path is unreachable here. Left as upstream wrote it to
  avoid vendor drift.
- AccessToken.build() returns ``''`` when app id/certificate are not 32-char
  hex. The coach service rejects an empty token with HTTP 503 rather than
  patching the vendor builder.
"""

from .RtcTokenBuilder2 import RtcTokenBuilder, Role_Publisher, Role_Subscriber

__all__ = ["RtcTokenBuilder", "Role_Publisher", "Role_Subscriber"]
