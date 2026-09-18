/**
 * 包迁移注册表 —— ScenarioPack 的存储槽位（0.3b）。
 *
 * The host initializes the selected driver only after product composition,
 * so every active pack can register migrations before any database opens.
 *
 * 本模块刻意不 import `storage/index.js`，避免反向依赖。
 */

import type { MigrationContribution } from '../scenario/pack.js';

interface RegisteredMigration {
  packId: string;
  migration: MigrationContribution;
}

const registered: RegisteredMigration[] = [];

/**
 * 注册一个包的迁移贡献。重复注册同一包会抛错（组装期笔误，fail fast）。
 */
export function registerPackMigrations(packId: string, migration: MigrationContribution): void {
  if (registered.some((r) => r.packId === packId)) {
    throw new Error(`[pack-migrations] duplicate registration for pack: ${packId}`);
  }
  registered.push({ packId, migration });
}

/** 已注册的全部迁移，按注册顺序返回。 */
export function getPackMigrations(): readonly RegisteredMigration[] {
  return registered;
}
