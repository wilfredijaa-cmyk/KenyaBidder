export class AppError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}
export const badRequest = (code, message) => new AppError(400, code, message);
export const forbidden = (code, message) => new AppError(403, code, message);
export const notFound = (code, message) => new AppError(404, code, message);
export const conflict = (code, message) => new AppError(409, code, message);

export const isInt = (n) => Number.isInteger(n);
export const isPosInt = (n) => Number.isInteger(n) && n > 0;
export const isNonNegInt = (n) => Number.isInteger(n) && n >= 0;
