/**
 * demo-flag：静态演示构建（steerableframework.com/demo/）的模块级开关。
 * 正常应用构建从不调用 markDemoMode()，所以缺省必须为 false；
 * 标记一旦置位不可逆（没有 unmark——页面生命周期内不需要）。
 */
import { describe, expect, it } from 'vitest';
import { isDemoMode, markDemoMode } from './demo-flag';

describe('demo-flag', () => {
  it('缺省 false，markDemoMode 后置 true 且保持', () => {
    expect(isDemoMode()).toBe(false);
    markDemoMode();
    expect(isDemoMode()).toBe(true);
    markDemoMode();
    expect(isDemoMode()).toBe(true);
  });
});
