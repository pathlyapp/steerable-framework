import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  userDataDir: '',
}));

vi.mock('../../src/runtime.js', () => ({
  getUserDataDir: () => mocks.userDataDir,
  getAppRootDir: () => mocks.userDataDir,
}));

import { setWorkspaceSkillRootsProvider } from '../../src/local-backend/skill-loader.js';
import {
  installSkillFromDirectory,
  maybeAutoInstallWrittenSkill,
  parseSkillNameFromMarkdown,
} from '../../src/local-backend/skill-install.js';

const SKILL_MD = `---
name: demo-skill
description: A demo skill for install tests.
---

# Demo
`;

beforeEach(() => {
  mocks.userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'skill-install-user-'));
  setWorkspaceSkillRootsProvider(null);
});

afterEach(() => {
  setWorkspaceSkillRootsProvider(null);
});

describe('parseSkillNameFromMarkdown', () => {
  it('reads frontmatter name and sanitizes', () => {
    expect(parseSkillNameFromMarkdown(SKILL_MD, 'fallback')).toBe('demo-skill');
  });

  it('falls back to directory name when frontmatter has no name', () => {
    expect(parseSkillNameFromMarkdown('# just a body\n', 'My Skill')).toBe('my-skill');
  });
});

describe('installSkillFromDirectory', () => {
  it('copies the skill dir into the user skills root', () => {
    const source = fs.mkdtempSync(path.join(os.tmpdir(), 'skill-install-src-'));
    const skillDir = path.join(source, 'demo-skill');
    fs.mkdirSync(skillDir);
    fs.writeFileSync(path.join(skillDir, 'SKILL.md'), SKILL_MD);
    const { name, dest } = installSkillFromDirectory(skillDir);
    expect(name).toBe('demo-skill');
    expect(fs.existsSync(path.join(dest, 'SKILL.md'))).toBe(true);
    expect(dest).toBe(path.join(mocks.userDataDir, 'skills', 'demo-skill'));
  });
});

describe('maybeAutoInstallWrittenSkill', () => {
  it('imports a SKILL.md written under an unknown skills/ folder', () => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'skill-install-ws-'));
    const skillDir = path.join(workspace, 'skills', 'demo-skill');
    fs.mkdirSync(skillDir, { recursive: true });
    const skillMd = path.join(skillDir, 'SKILL.md');
    fs.writeFileSync(skillMd, SKILL_MD);
    expect(maybeAutoInstallWrittenSkill(skillMd)).toEqual({ name: 'demo-skill' });
    expect(fs.existsSync(path.join(mocks.userDataDir, 'skills', 'demo-skill', 'SKILL.md'))).toBe(
      true,
    );
  });

  it('does not copy when the parent skills/ dir is already a listed root', () => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'skill-install-listed-'));
    const skillsRoot = path.join(workspace, 'skills');
    const skillDir = path.join(skillsRoot, 'demo-skill');
    fs.mkdirSync(skillDir, { recursive: true });
    const skillMd = path.join(skillDir, 'SKILL.md');
    fs.writeFileSync(skillMd, SKILL_MD);
    setWorkspaceSkillRootsProvider(() => [skillsRoot]);
    expect(maybeAutoInstallWrittenSkill(skillMd)).toBeNull();
    expect(fs.existsSync(path.join(mocks.userDataDir, 'skills', 'demo-skill'))).toBe(false);
  });

  it('ignores writes that are not SKILL.md', () => {
    const file = path.join(os.tmpdir(), 'notes.md');
    fs.writeFileSync(file, 'hi');
    expect(maybeAutoInstallWrittenSkill(file)).toBeNull();
  });
});
