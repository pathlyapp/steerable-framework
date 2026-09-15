/**
 * Copy a skill directory into the user skills root (settings import +
 * auto-register when the agent writes a SKILL.md outside known roots).
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { getSkillsDir, getUserSkillsDir, listSkillRoots } from './skill-loader.js';

export function resolveUserPath(inputPath: string): string {
  const expanded = inputPath.startsWith('~')
    ? path.join(os.homedir(), inputPath.slice(1))
    : inputPath;
  return path.resolve(expanded);
}

export function parseSkillNameFromMarkdown(
  skillMdContent: string,
  fallbackDirName: string,
): string {
  let skillName = '';
  if (skillMdContent.startsWith('---')) {
    const rest = skillMdContent.slice(3);
    const end = rest.indexOf('\n---');
    if (end !== -1) {
      const fmRaw = rest.slice(0, end);
      const nameMatch = fmRaw.match(/^name:\s*(.+)$/m);
      if (nameMatch?.[1]) {
        skillName = nameMatch[1].trim().replace(/^["']|["']$/g, '');
      }
    }
  }
  if (!skillName) skillName = fallbackDirName;
  return skillName.toLowerCase().replace(/[^a-z0-9-]/g, '-');
}

export function installSkillFromDirectory(sourceDir: string): { name: string; dest: string } {
  const skillMdPath = path.join(sourceDir, 'SKILL.md');
  if (!fs.existsSync(skillMdPath) || !fs.statSync(skillMdPath).isFile()) {
    throw new Error(`未找到技能文件，请检查路径中是否存在 SKILL.md 文件: ${sourceDir}`);
  }
  const skillName = parseSkillNameFromMarkdown(
    fs.readFileSync(skillMdPath, 'utf8'),
    path.basename(sourceDir),
  );
  const dest = path.join(getUserSkillsDir(), skillName);
  fs.mkdirSync(dest, { recursive: true });
  fs.cpSync(sourceDir, dest, { recursive: true });
  return { name: skillName, dest };
}

function isInside(candidate: string, root: string): boolean {
  const resolved = path.resolve(candidate);
  const resolvedRoot = path.resolve(root);
  return resolved === resolvedRoot || resolved.startsWith(resolvedRoot + path.sep);
}

/**
 * After a successful write/edit of SKILL.md: if that skill dir is not
 * already on a listed root (builtin / workspace / user), copy it into
 * the user skills directory so Skill 设置 and `/` pick it up without a
 * manual import. No-ops for anything that isn't `…/skills/<name>/SKILL.md`.
 */
export function maybeAutoInstallWrittenSkill(writtenPath: string): { name: string } | null {
  const filePath = resolveUserPath(writtenPath);
  if (path.basename(filePath) !== 'SKILL.md') return null;
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) return null;

  const skillDir = path.dirname(filePath);
  const root = path.dirname(skillDir);
  if (path.basename(root) !== 'skills') return null;
  if (isInside(skillDir, getUserSkillsDir()) || isInside(skillDir, getSkillsDir())) {
    return null;
  }
  if (listSkillRoots().some((listed) => path.resolve(listed) === path.resolve(root))) {
    return null;
  }
  return { name: installSkillFromDirectory(skillDir).name };
}
