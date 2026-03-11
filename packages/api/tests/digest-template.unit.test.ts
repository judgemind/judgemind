/**
 * Unit tests for the digest email template rendering.
 * No database connection needed — tests pure template logic.
 */

import { describe, it, expect } from 'vitest';
import { renderDigestEmail } from '../src/email/templates/digest';
import type { DigestAlertItem } from '../src/email/templates/digest';

describe('renderDigestEmail', () => {
  const sampleAlerts: DigestAlertItem[] = [
    {
      alertType: 'judge_ruling',
      caseNumber: '24NNCV02551',
      caseTitle: 'Smith v. Jones',
      judgeName: 'Crowfoot, William A.',
      hearingDate: '2026-03-15',
      outcome: 'granted',
      motionType: 'msj',
      excerpt: 'The court grants the motion for summary judgment.',
    },
    {
      alertType: 'keyword',
      caseNumber: '24STCV01234',
      caseTitle: 'Doe v. Acme',
    },
  ];

  it('renders subject with date and alert count', () => {
    const { subject } = renderDigestEmail({
      alerts: sampleAlerts,
      digestDate: '2026-03-15',
    });
    expect(subject).toContain('2026-03-15');
    expect(subject).toContain('2 alerts');
  });

  it('renders singular alert count', () => {
    const { subject } = renderDigestEmail({
      alerts: [sampleAlerts[0]],
      digestDate: '2026-03-15',
    });
    expect(subject).toContain('1 alert');
    expect(subject).not.toContain('1 alerts');
  });

  it('includes display name in greeting when provided', () => {
    const { text, html } = renderDigestEmail({
      displayName: 'Drew',
      alerts: sampleAlerts,
      digestDate: '2026-03-15',
    });
    expect(text).toContain('Hi Drew,');
    expect(html).toContain('Hi Drew,');
  });

  it('uses generic greeting when display name is absent', () => {
    const { text, html } = renderDigestEmail({
      alerts: sampleAlerts,
      digestDate: '2026-03-15',
    });
    expect(text).toContain('Hi,');
    expect(html).toContain('Hi,');
  });

  it('includes case details in plain text', () => {
    const { text } = renderDigestEmail({
      alerts: sampleAlerts,
      digestDate: '2026-03-15',
    });
    expect(text).toContain('24NNCV02551');
    expect(text).toContain('Smith v. Jones');
    expect(text).toContain('Crowfoot, William A.');
    expect(text).toContain('granted');
    expect(text).toContain('msj');
    expect(text).toContain('[Judge Ruling]');
    expect(text).toContain('[Keyword Match]');
  });

  it('includes case details in HTML', () => {
    const { html } = renderDigestEmail({
      alerts: sampleAlerts,
      digestDate: '2026-03-15',
    });
    expect(html).toContain('24NNCV02551');
    expect(html).toContain('Smith v. Jones');
    expect(html).toContain('Crowfoot, William A.');
    expect(html).toContain('Judge Ruling');
    expect(html).toContain('Keyword Match');
  });

  it('includes excerpt when provided', () => {
    const { text, html } = renderDigestEmail({
      alerts: sampleAlerts,
      digestDate: '2026-03-15',
    });
    expect(text).toContain('The court grants the motion');
    expect(html).toContain('The court grants the motion');
  });

  it('includes link to alerts page', () => {
    const { text, html } = renderDigestEmail({
      alerts: sampleAlerts,
      digestDate: '2026-03-15',
    });
    expect(text).toContain('https://judgemind.org/alerts');
    expect(html).toContain('https://judgemind.org/alerts');
  });
});
