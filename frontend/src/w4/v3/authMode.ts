/** Server configuration is authoritative; default closed until auth bootstrap. */
let emailCodeAuth = process.env.NEXT_PUBLIC_EMAIL_CODE_AUTH !== "0";
export function emailCodeAuthEnabled(): boolean { return emailCodeAuth; }
export function configureEmailCodeAuth(enabled: boolean): void { emailCodeAuth = enabled; }
