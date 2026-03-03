import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { SendEmailCommand } from '@aws-sdk/client-ses';

// vi.mock is hoisted to the top of the file by vitest, so any variables it
// references must also be hoisted via vi.hoisted() to avoid TDZ errors.
const mockSend = vi.hoisted(() => vi.fn().mockResolvedValue({}));

vi.mock('../src/email/client', () => ({
  sesClient: { send: mockSend },
}));

// Import after mocks are set up.
import { sendEmail } from '../src/email/send';
import { renderVerificationEmail } from '../src/email/templates/verification';
import { renderPasswordResetEmail } from '../src/email/templates/password-reset';

describe('sendEmail', () => {
  beforeEach(() => {
    mockSend.mockClear();
  });

  afterEach(() => {
    delete process.env.EMAIL_FROM;
    delete process.env.SES_CONFIGURATION_SET;
  });

  it('calls sesClient.send with correct To, Subject, HTML, and text', async () => {
    await sendEmail({
      to: 'user@example.com',
      subject: 'Hello',
      htmlBody: '<p>Hi</p>',
      textBody: 'Hi',
    });

    expect(mockSend).toHaveBeenCalledOnce();
    const cmd: SendEmailCommand = mockSend.mock.calls[0][0];
    expect(cmd.input.Destination?.ToAddresses).toContain('user@example.com');
    expect(cmd.input.Message?.Subject?.Data).toBe('Hello');
    expect(cmd.input.Message?.Body?.Html?.Data).toBe('<p>Hi</p>');
    expect(cmd.input.Message?.Body?.Text?.Data).toBe('Hi');
  });

  it('defaults FROM address to no-reply@judgemind.org', async () => {
    delete process.env.EMAIL_FROM;
    await sendEmail({ to: 'a@b.com', subject: 'x', htmlBody: '<p>x</p>', textBody: 'x' });
    const cmd: SendEmailCommand = mockSend.mock.calls[0][0];
    expect(cmd.input.Source).toBe('no-reply@judgemind.org');
  });

  it('uses EMAIL_FROM env var when set', async () => {
    process.env.EMAIL_FROM = 'alerts@judgemind.org';
    await sendEmail({ to: 'a@b.com', subject: 'x', htmlBody: '<p>x</p>', textBody: 'x' });
    const cmd: SendEmailCommand = mockSend.mock.calls[0][0];
    expect(cmd.input.Source).toBe('alerts@judgemind.org');
  });

  it('omits ConfigurationSetName when SES_CONFIGURATION_SET is not set', async () => {
    delete process.env.SES_CONFIGURATION_SET;
    await sendEmail({ to: 'a@b.com', subject: 'x', htmlBody: '<p>x</p>', textBody: 'x' });
    const cmd: SendEmailCommand = mockSend.mock.calls[0][0];
    expect(cmd.input.ConfigurationSetName).toBeUndefined();
  });

  it('includes ConfigurationSetName when SES_CONFIGURATION_SET is set', async () => {
    process.env.SES_CONFIGURATION_SET = 'judgemind-dev';
    await sendEmail({ to: 'a@b.com', subject: 'x', htmlBody: '<p>x</p>', textBody: 'x' });
    const cmd: SendEmailCommand = mockSend.mock.calls[0][0];
    expect(cmd.input.ConfigurationSetName).toBe('judgemind-dev');
  });
});

describe('renderVerificationEmail', () => {
  it('returns correct subject', () => {
    const { subject } = renderVerificationEmail({ verificationUrl: 'https://judgemind.org/verify?token=abc' });
    expect(subject).toBe('Verify your Judgemind account');
  });

  it('includes the verification URL in both html and text', () => {
    const url = 'https://judgemind.org/verify?token=abc123';
    const { html, text } = renderVerificationEmail({ verificationUrl: url });
    expect(html).toContain(url);
    expect(text).toContain(url);
  });

  it('includes displayName in the greeting when provided', () => {
    const { html, text } = renderVerificationEmail({
      verificationUrl: 'https://judgemind.org/verify',
      displayName: 'Alice',
    });
    expect(html).toContain('Hi Alice,');
    expect(text).toContain('Hi Alice,');
  });

  it('uses generic greeting when displayName is omitted', () => {
    const { html, text } = renderVerificationEmail({ verificationUrl: 'https://judgemind.org/verify' });
    expect(html).toContain('Hi,');
    expect(text).toContain('Hi,');
  });

  it('mentions 24 hour expiry in both html and text', () => {
    const { html, text } = renderVerificationEmail({ verificationUrl: 'https://judgemind.org/verify' });
    expect(html).toContain('24 hours');
    expect(text).toContain('24 hours');
  });
});

describe('renderPasswordResetEmail', () => {
  it('returns correct subject', () => {
    const { subject } = renderPasswordResetEmail({ resetUrl: 'https://judgemind.org/reset?token=xyz' });
    expect(subject).toBe('Reset your Judgemind password');
  });

  it('includes the reset URL in both html and text', () => {
    const url = 'https://judgemind.org/reset?token=xyz789';
    const { html, text } = renderPasswordResetEmail({ resetUrl: url });
    expect(html).toContain(url);
    expect(text).toContain(url);
  });

  it('includes displayName in the greeting when provided', () => {
    const { html, text } = renderPasswordResetEmail({
      resetUrl: 'https://judgemind.org/reset',
      displayName: 'Bob',
    });
    expect(html).toContain('Hi Bob,');
    expect(text).toContain('Hi Bob,');
  });

  it('mentions 1 hour expiry in both html and text', () => {
    const { html, text } = renderPasswordResetEmail({ resetUrl: 'https://judgemind.org/reset' });
    expect(html).toContain('1 hour');
    expect(text).toContain('1 hour');
  });
});
