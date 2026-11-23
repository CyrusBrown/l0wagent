// Errors raised by the protocol modules; each carries the protocol error `code` where one applies.
import { VERSION } from "./consts";

export class ProtocolError extends Error {
  static defaultCode = "bad_request";
  code: string;
  detail: string;
  constructor(detail = "", code?: string) {
    super(detail);
    this.detail = detail;
    this.code = code ?? (new.target as typeof ProtocolError).defaultCode;
  }
  body(): ErrorBody {
    return errorBody(this.code, this.detail);
  }
}

export class EncodingError extends ProtocolError {
  static override defaultCode = "bad_encoding";
}
export class KeyFormatError extends ProtocolError {
  static override defaultCode = "bad_key";
}
export class SignatureError extends ProtocolError {
  static override defaultCode = "bad_signature";
}
export class DocumentError extends ProtocolError {
  static override defaultCode = "bad_document";
}
export class UnsupportedVersion extends DocumentError {
  static override defaultCode = "unsupported_version";
}
export class ValidationError extends DocumentError {
  static override defaultCode = "bad_document";
}
export class NameRuleError extends ProtocolError {
  static override defaultCode = "bad_registration";
}
export class InviteError extends ProtocolError {
  static override defaultCode = "bad_invite";
}
export class FrameError extends ProtocolError {
  static override defaultCode = "bad_frame";
}

/** Sealing or opening failed (HPKE). The detail never says why an open failed. */
export class SealError extends ProtocolError {
  static override defaultCode = "bad_seal";
}

export interface ErrorBody {
  v: string;
  error: string;
  detail: string;
  [extra: string]: unknown;
}

export function errorBody(code: string, detail = ""): ErrorBody {
  return { v: VERSION, error: code, detail };
}
