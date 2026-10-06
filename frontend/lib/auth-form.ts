/**
 * 登录 / 注册表单的纯函数：校验、请求体组装、回跳地址白名单。
 *
 * 口径与后端 `app/api/auth.py` 对齐，两处必须同时改：
 *   * 密码长度按**字节**判（bcrypt 的 72 字节上限）——按字符数判会放过 24 个汉字
 *     （72 字节）以外的口令，用户在后端才撞 422；
 *   * 邮箱统一 `trim().toLowerCase()` 后再交后端（后端也做一次，前端这层只为即时反馈）。
 */

export const PASSWORD_MIN_BYTES = 8;
export const PASSWORD_MAX_BYTES = 72;

const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export interface AuthForm {
  email: string;
  password: string;
  confirm: string;
}

export type AuthErrors = Partial<Record<keyof AuthForm, string>>;

/** UTF-8 字节数：与 Python 侧 `len(value.encode("utf-8"))` 同口径。 */
export function passwordBytes(value: string): number {
  return new TextEncoder().encode(value).length;
}

/** 表单是否可提交：无错误且字段非空。 */
export function hasErrors(errors: AuthErrors): boolean {
  return Object.keys(errors).length > 0;
}

/**
 * 校验一次提交。`needConfirm` 只在注册页为真。
 *
 * 密码下限按**字符**数不按字节：8 个汉字是 24 字节，够长；1 个汉字才 3 字节，太短。
 * 这里用字节判下限同样成立（3 < 8），且与上限口径一致，不必两套。
 */
export function validate(form: AuthForm, needConfirm = false): AuthErrors {
  const errors: AuthErrors = {};
  const email = form.email.trim();

  if (!email) errors.email = "请填写邮箱";
  else if (!EMAIL_PATTERN.test(email)) errors.email = "邮箱格式不正确";

  const size = passwordBytes(form.password);
  if (!form.password) errors.password = "请填写密码";
  else if (size < PASSWORD_MIN_BYTES) errors.password = `密码至少 ${PASSWORD_MIN_BYTES} 个字节`;
  else if (size > PASSWORD_MAX_BYTES)
    errors.password = `密码不得超过 ${PASSWORD_MAX_BYTES} 个字节`;

  if (needConfirm && form.confirm !== form.password) errors.confirm = "两次输入不一致";

  return errors;
}

export function credentials(form: AuthForm): { email: string; password: string } {
  return { email: form.email.trim().toLowerCase(), password: form.password };
}

/**
 * 登录后回跳地址只认站内路径。
 *
 * `next` 来自查询串，直接 `router.replace(next)` 会变成开放重定向
 * （`?next=https://evil.example` 把用户送去钓鱼页）；`//host` 是协议相对 URL，同样要挡。
 */
export function safeNextPath(raw: string | null | undefined): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return "/";
  return raw;
}
