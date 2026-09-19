import { LuCheck, LuLoaderCircle } from 'react-icons/lu';

interface SettingsSaveButtonProps {
  saving: boolean;
  savedOk: boolean;
  disabled?: boolean;
  onClick: () => void;
  testId?: string;
}

/** 设置页标题栏 / 本地模型表单共用的保存按钮。 */
export function SettingsSaveButton({
  saving,
  savedOk,
  disabled,
  onClick,
  testId,
}: SettingsSaveButtonProps) {
  return (
    <button
      type="button"
      data-testid={testId}
      onClick={onClick}
      disabled={disabled}
      className="flex h-7 items-center gap-1.5 rounded-full bg-agent-foreground px-3 text-xs font-medium text-agent-canvas transition-all hover:opacity-90 disabled:opacity-50"
    >
      {saving ? (
        <LuLoaderCircle className="h-3.5 w-3.5 animate-spin" />
      ) : savedOk ? (
        <LuCheck className="h-3.5 w-3.5" />
      ) : null}
      保存
    </button>
  );
}
