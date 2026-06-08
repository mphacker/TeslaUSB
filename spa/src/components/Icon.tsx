import type { JSX } from "preact";

/** Path to the carried-over Lucide sprite (served at /static by webd + Vite). */
const SPRITE = "/static/icons/lucide-sprite.svg";

interface IconProps extends JSX.SVGAttributes<SVGSVGElement> {
  /** Sprite symbol name without the `icon-` prefix, e.g. "map-pin". */
  name: string;
}

/**
 * Renders a Lucide sprite icon via `<use href="…#icon-NAME">`, matching the
 * legacy templates' `nav-icon` markup so existing CSS sizing applies.
 */
export function Icon({ name, class: cls, ...rest }: IconProps) {
  return (
    <svg class={cls ?? "nav-icon"} aria-hidden="true" {...rest}>
      <use href={`${SPRITE}#icon-${name}`} />
    </svg>
  );
}
