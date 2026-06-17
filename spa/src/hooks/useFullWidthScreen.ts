import { useEffect } from "preact/hooks";
import "../styles/fullwidth.css";

/**
 * Opts the current screen out of the global 1200px `.main-content` cap so its
 * `.container` card fills the available browser width — matching the Music
 * screen. Scoped via the `screen-fullwidth` body class, added while the screen
 * is mounted and removed on unmount (mirrors the `.music-active` pattern).
 *
 * Reference-counted so navigating between two full-width screens (or a
 * remount) never strips the class while one is still on screen.
 */
let fullWidthCount = 0;

export function useFullWidthScreen(): void {
  useEffect(() => {
    fullWidthCount += 1;
    document.body.classList.add("screen-fullwidth");
    return () => {
      fullWidthCount -= 1;
      if (fullWidthCount <= 0) {
        fullWidthCount = 0;
        document.body.classList.remove("screen-fullwidth");
      }
    };
  }, []);
}
