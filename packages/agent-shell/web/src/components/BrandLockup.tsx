import { BRAND_NAME, BRAND_TITLE, getBrandLogoUrl } from '@/brand';

/**
 * 侧栏品牌锁头。配了 brand.title 就 logo + 标题；没配标题则只显示 logo，
 * 并按原图宽高比缩放（一体字标不要压成方图）。
 */
export function BrandLockup() {
  const title = BRAND_TITLE.trim();
  return (
    <div className="flex min-w-0 items-center gap-2">
      <img
        src={getBrandLogoUrl()}
        alt={title || BRAND_NAME}
        className="h-6 w-auto max-w-[10rem] flex-shrink-0 select-none object-contain object-left"
        draggable={false}
      />
      {title ? (
        <span className="truncate text-xs font-semibold tracking-tight text-agent-foreground">
          {title}
        </span>
      ) : null}
    </div>
  );
}
