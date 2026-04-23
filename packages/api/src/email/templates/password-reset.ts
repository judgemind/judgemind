export interface PasswordResetTemplateParams {
  resetUrl: string;
  displayName?: string;
}

export function renderPasswordResetEmail({
  resetUrl,
  displayName,
}: PasswordResetTemplateParams): { subject: string; html: string; text: string } {
  const greeting = displayName ? `Hi ${displayName},` : 'Hi,';
  const subject = 'Reset your Judgemind password';

  const text = `${greeting}

We received a request to reset the password for your Judgemind account.

Click the link below to choose a new password:

  ${resetUrl}

This link expires in 1 hour. If you did not request a password reset, you can safely ignore this email — your password will not change.

— The Judgemind Team`;

  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>${subject}</title>
</head>
<body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px; color: #1a1a1a;">
  <p>${greeting}</p>
  <p>We received a request to reset the password for your Judgemind account.</p>
  <p>Click the button below to choose a new password:</p>
  <p style="text-align: center; margin: 32px 0;">
    <a href="${resetUrl}"
       style="background-color: #1a56db; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 4px; display: inline-block; font-weight: bold;">
      Reset Password
    </a>
  </p>
  <p style="color: #555; font-size: 14px;">Or copy and paste this link into your browser:</p>
  <p style="font-size: 14px; word-break: break-all; color: #1a56db;">${resetUrl}</p>
  <p style="color: #555; font-size: 14px;">This link expires in 1 hour. If you did not request a password reset, you can safely ignore this email — your password will not change.</p>
  <p>— The Judgemind Team</p>
</body>
</html>`;

  return { subject, html, text };
}
