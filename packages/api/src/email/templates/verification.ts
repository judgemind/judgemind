export interface VerificationTemplateParams {
  verificationUrl: string;
  displayName?: string;
}

export function renderVerificationEmail({
  verificationUrl,
  displayName,
}: VerificationTemplateParams): { subject: string; html: string; text: string } {
  const greeting = displayName ? `Hi ${displayName},` : 'Hi,';
  const subject = 'Verify your Judgemind account';

  const text = `${greeting}

Welcome to Judgemind — the free, open-source legal research platform.

Please verify your email address by visiting:

  ${verificationUrl}

This link expires in 24 hours. If you did not create a Judgemind account, you can safely ignore this email.

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
  <p>Welcome to <strong>Judgemind</strong> — the free, open-source legal research platform.</p>
  <p>Please verify your email address by clicking the button below:</p>
  <p style="text-align: center; margin: 32px 0;">
    <a href="${verificationUrl}"
       style="background-color: #1a56db; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 4px; display: inline-block; font-weight: bold;">
      Verify Email Address
    </a>
  </p>
  <p style="color: #555; font-size: 14px;">Or copy and paste this link into your browser:</p>
  <p style="font-size: 14px; word-break: break-all; color: #1a56db;">${verificationUrl}</p>
  <p style="color: #555; font-size: 14px;">This link expires in 24 hours. If you did not create a Judgemind account, you can safely ignore this email.</p>
  <p>— The Judgemind Team</p>
</body>
</html>`;

  return { subject, html, text };
}
