import { useEffect, useRef, useState } from "preact/hooks";

interface BusyOverlayProps {
  block: boolean;
  visible: boolean;
  title?: string;
  message?: string;
}

export function useDelayedFlag(active: boolean, delayMs: number): boolean {
  const [shown, setShown] = useState(false);

  useEffect(() => {
    if (!active) {
      setShown(false);
      return;
    }
    const timer = setTimeout(() => setShown(true), delayMs);
    return () => clearTimeout(timer);
  }, [active, delayMs]);

  return shown;
}

export function BusyOverlay({ block, visible, title, message }: BusyOverlayProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  // While the overlay is up it must block every other action — including
  // keyboard: move focus onto the (non-interactive) backdrop and swallow Tab so
  // focus can't reach controls behind the pointer-blocking layer. On teardown,
  // restore focus to whatever the user had focused before we grabbed it.
  useEffect(() => {
    if (!block) return;
    restoreFocusRef.current = document.activeElement as HTMLElement | null;
    rootRef.current?.focus();
    return () => {
      const prev = restoreFocusRef.current;
      if (prev && typeof prev.focus === "function" && prev.isConnected) {
        prev.focus();
      }
    };
  }, [block]);

  if (!block) return null;

  const trapTab = (e: KeyboardEvent) => {
    if (e.key === "Tab") {
      e.preventDefault();
      rootRef.current?.focus();
    }
  };

  return (
    <div
      ref={rootRef}
      tabIndex={-1}
      class={`media-page busy-overlay-backdrop${visible ? " busy-overlay-visible" : ""}`}
      data-testid="busy-overlay"
      role="dialog"
      aria-modal="true"
      aria-busy="true"
      aria-label={title ?? "Working…"}
      onKeyDown={trapTab}
    >
      {visible && (
        <div class="busy-overlay-card" data-testid="busy-overlay-card">
          <span class="busy-overlay-spinner" aria-hidden="true" />
          <h3 class="busy-overlay-title">{title ?? "Working…"}</h3>
          <p class="busy-overlay-message" aria-live="assertive">
            {message ?? "Please wait — finishing your last action."}
          </p>
        </div>
      )}
    </div>
  );
}
