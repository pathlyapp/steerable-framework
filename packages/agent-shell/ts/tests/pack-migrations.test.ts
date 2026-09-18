/**
 * 包迁移注册表（0.3b）的纯模块单测。
 *
 * LocalStore 依赖 better-sqlite3 native module，vitest 里无法构造真实
 * 实例；这里只验证注册表本身的契约（按序列举、重复注册 fail fast）。
 * DDL 应用路径由消费方产品的真实启动覆盖。
 */

import { describe, expect, it } from 'vitest';

import { getPackMigrations, registerPackMigrations } from '../src/storage/pack-migrations.js';

describe('pack migrations registry', () => {
  it('registers and lists in registration order', () => {
    registerPackMigrations('demo-a', { ddl: ['CREATE TABLE IF NOT EXISTS demo_a (id TEXT)'] });
    registerPackMigrations('demo-b', { ensureColumns: [{ table: 'demo_a', column: 'x', type: 'INTEGER' }] });
    const ids = getPackMigrations().map((r) => r.packId);
    expect(ids.indexOf('demo-a')).toBeLessThan(ids.indexOf('demo-b'));
    expect(getPackMigrations().find((r) => r.packId === 'demo-b')?.migration.ensureColumns).toHaveLength(1);
  });

  it('rejects duplicate pack registration', () => {
    registerPackMigrations('demo-dup', {});
    expect(() => registerPackMigrations('demo-dup', {})).toThrow(/duplicate/);
  });
});
