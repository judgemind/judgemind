import { describe, it, expect } from 'vitest';
import { parseDatabaseUrl } from '../src/data-access/parse-database-url';

describe('parseDatabaseUrl', () => {
  it('returns original URL and ssl: false when no sslmode parameter', () => {
    const url = 'postgresql://user:pass@localhost:5432/mydb';
    const result = parseDatabaseUrl(url);

    expect(result.connectionString).toBe(url);
    expect(result.ssl).toBe(false);
  });

  it('strips sslmode=require and returns ssl config', () => {
    const url = 'postgresql://user:pass@host:5432/mydb?sslmode=require';
    const result = parseDatabaseUrl(url);

    expect(result.connectionString).toBe('postgresql://user:pass@host:5432/mydb');
    expect(result.ssl).toEqual({ rejectUnauthorized: false });
  });

  it('strips sslmode when it appears among other query params', () => {
    const url =
      'postgresql://user:pass@host:5432/mydb?sslmode=require&connect_timeout=10';
    const result = parseDatabaseUrl(url);

    expect(result.connectionString).toBe(
      'postgresql://user:pass@host:5432/mydb?connect_timeout=10',
    );
    expect(result.ssl).toEqual({ rejectUnauthorized: false });
  });

  it('strips sslmode when it is not the first query param', () => {
    const url =
      'postgresql://user:pass@host:5432/mydb?connect_timeout=10&sslmode=require';
    const result = parseDatabaseUrl(url);

    expect(result.connectionString).toBe(
      'postgresql://user:pass@host:5432/mydb?connect_timeout=10',
    );
    expect(result.ssl).toEqual({ rejectUnauthorized: false });
  });

  it('strips sslmode when it is in the middle of other query params', () => {
    const url =
      'postgresql://user:pass@host:5432/mydb?connect_timeout=10&sslmode=require&application_name=app';
    const result = parseDatabaseUrl(url);

    expect(result.connectionString).toBe(
      'postgresql://user:pass@host:5432/mydb?connect_timeout=10&application_name=app',
    );
    expect(result.ssl).toEqual({ rejectUnauthorized: false });
  });

  it('handles sslmode=verify-full the same as sslmode=require', () => {
    const url = 'postgresql://user:pass@host:5432/mydb?sslmode=verify-full';
    const result = parseDatabaseUrl(url);

    expect(result.connectionString).toBe('postgresql://user:pass@host:5432/mydb');
    expect(result.ssl).toEqual({ rejectUnauthorized: false });
  });

  it('strips trailing ? after removing sslmode as only param', () => {
    const url = 'postgresql://user:pass@host:5432/mydb?sslmode=require';
    const result = parseDatabaseUrl(url);

    expect(result.connectionString).not.toMatch(/\?$/);
  });
});
