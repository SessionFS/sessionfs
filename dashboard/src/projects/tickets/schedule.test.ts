import { describe, expect, it } from 'vitest';
import { ApiError } from '../../api/client';
import { ESTIMATE_RE, estimateError, formatDueDate, isOverdue, scheduleErrorMessage, scheduleFieldErrors, todayIso } from './schedule';

describe('estimate validation', () => {
  it.each(['2d 4h', '2d4h', '30m', '1w 2d', '  3h  ', '1w2d3h4m'])('accepts %s', (v) => {
    expect(ESTIMATE_RE.test(v)).toBe(true);
    expect(estimateError(v)).toBeNull();
  });

  it.each(['2 days', 'd2', '2x', '1.5h', 'soon', '-1d'])('rejects %s', (v) => {
    expect(estimateError(v)).toMatch(/Jira durations/);
  });

  it('asks for a value instead of sending an empty one', () => {
    expect(estimateError('   ')).toMatch(/Clear/);
  });

  it('caps the length like the server', () => {
    expect(estimateError('1d '.repeat(12))).toMatch(/32 characters/);
  });
});

describe('overdue', () => {
  const today = '2026-10-28';
  it('is past due and not terminal', () => {
    expect(isOverdue({ status: 'open', due_date: '2026-10-27' }, today)).toBe(true);
    expect(isOverdue({ status: 'blocked', due_date: '2026-01-01' }, today)).toBe(true);
  });
  it('is never due today, in the future, unset, or on finished work', () => {
    expect(isOverdue({ status: 'open', due_date: today }, today)).toBe(false);
    expect(isOverdue({ status: 'open', due_date: '2026-11-01' }, today)).toBe(false);
    expect(isOverdue({ status: 'open', due_date: null }, today)).toBe(false);
    for (const status of ['done', 'closed', 'cancelled']) {
      expect(isOverdue({ status, due_date: '2026-10-01' }, today)).toBe(false);
    }
  });
  it('uses the UTC calendar date, like the server', () => {
    expect(todayIso(new Date('2026-01-05T23:59:00Z'))).toBe('2026-01-05');
    expect(todayIso(new Date('2026-01-06T00:01:00Z'))).toBe('2026-01-06');
  });
});

describe('formatDueDate', () => {
  const now = new Date(2026, 9, 28);
  it('drops the year in the current year and keeps it otherwise', () => {
    expect(formatDueDate('2026-11-15', now)).toBe('Nov 15');
    expect(formatDueDate('2027-01-02', now)).toBe('Jan 2, 2027');
  });
  it('does not shift a calendar date across time zones', () => {
    expect(formatDueDate('2026-11-01', now)).toBe('Nov 1');
  });
});

describe('scheduleErrorMessage', () => {
  it('surfaces the field message from a 422', () => {
    const body = JSON.stringify({
      error: {
        code: '422',
        message: 'Validation error',
        details: {
          errors: [
            { loc: ['body', 'estimated_effort'], msg: "Value error, estimated_effort must be a Jira duration like '2d 4h', '30m' or '1w 2d'", type: 'value_error' },
          ],
        },
      },
    });
    expect(scheduleErrorMessage(new ApiError(422, body))).toBe(
      "estimated_effort must be a Jira duration like '2d 4h', '30m' or '1w 2d'",
    );
  });
  it('routes field messages to the field they name', () => {
    const body = JSON.stringify({
      error: {
        code: '422',
        message: 'Validation error',
        details: {
          errors: [
            { loc: ['body', 'due_date'], msg: 'Input should be a valid date', type: 'date_parsing' },
            { loc: ['body', 'estimated_effort'], msg: 'Value error, estimated_effort cannot be empty', type: 'value_error' },
          ],
        },
      },
    });
    expect(scheduleFieldErrors(new ApiError(422, body))).toEqual({
      due_date: 'Input should be a valid date',
      estimated_effort: 'estimated_effort cannot be empty',
    });
    expect(scheduleFieldErrors(new Error('nope'))).toEqual({});
  });

  it('falls back to the usual message', () => {
    const body = JSON.stringify({ error: { code: 'ticket_edit_forbidden', message: 'Only the creator may edit this.' } });
    expect(scheduleErrorMessage(new ApiError(403, body))).toBe('Only the creator may edit this.');
  });
});
