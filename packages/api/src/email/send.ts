import { SendEmailCommand } from '@aws-sdk/client-ses';
import { sesClient } from './client';

export interface SendEmailParams {
  to: string;
  subject: string;
  htmlBody: string;
  textBody: string;
}

export async function sendEmail({ to, subject, htmlBody, textBody }: SendEmailParams): Promise<void> {
  const source = process.env.EMAIL_FROM ?? 'no-reply@judgemind.org';
  const configurationSet = process.env.SES_CONFIGURATION_SET;

  await sesClient.send(
    new SendEmailCommand({
      Source: source,
      Destination: { ToAddresses: [to] },
      Message: {
        Subject: { Data: subject, Charset: 'UTF-8' },
        Body: {
          Html: { Data: htmlBody, Charset: 'UTF-8' },
          Text: { Data: textBody, Charset: 'UTF-8' },
        },
      },
      ...(configurationSet ? { ConfigurationSetName: configurationSet } : {}),
    }),
  );
}
