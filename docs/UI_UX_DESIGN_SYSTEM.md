# TeslaUSB UI/UX Design System

Design rules and conventions for the TeslaUSB web interface. This document is the source of truth for all frontend work — follow these guidelines when building or modifying any page, component, or interaction.

---

## Design Philosophy

### Progressive Disclosure — Simple by Default, Powerful on Demand

The UI uses a three-layer approach so casual users see a clean interface while power users can access everything within 2 taps:

| Layer          | What the User Sees                                      | Interaction Required                         |
| -------------- | ------------------------------------------------------- | -------------------------------------------- |
| **Glanceable** | Map with recent trip, mode badge, trip summary          | None — visible on load                       |
| **One Tap**    | Video overlay, trip cycling, tab content, device status | Single tap/click                             |
| **Deep Dive**  | Video grid, folder browser, scheduler, system settings  | Deliberate action (toggle, expand, navigate) |

**Rule:** If a feature is used by >50% of users, surface it at Layer 1 or 2. Everything else goes to Layer 3 — accessible but not cluttering the default view.

---

## Color System

All colors are defined as CSS custom properties on `:root` (light) and `[data-theme="dark"]` (dark). Never use hardcoded hex values in component styles — always reference tokens.

### Dark Mode (Default)

| Token              | Hex       | Usage                                 |
| ------------------ | --------- | ------------------------------------- |
| `--bg-primary`     | `#0F172A` | Page background                       |
| `--bg-secondary`   | `#1E293B` | Card backgrounds                      |
| `--bg-tertiary`    | `#334155` | Elevated surfaces, input backgrounds  |
| `--text-primary`   | `#F8FAFC` | Primary text                          |
| `--text-secondary` | `#94A3B8` | Secondary/supporting text             |
| `--text-muted`     | `#64748B` | Muted/disabled text                   |
| `--accent-primary` | `#3B82F6` | Primary actions, links, active states |
| `--accent-success` | `#22C55E` | Success indicators, present mode      |
| `--accent-warning` | `#F59E0B` | Warnings, edit mode                   |
| `--accent-danger`  | `#EF4444` | Destructive actions, errors           |
| `--accent-info`    | `#06B6D4` | Informational elements                |
| `--border`         | `#334155` | Borders                               |
| `--border-subtle`  | `#1E293B` | Subtle dividers                       |

### Light Mode

| Token              | Hex       | Usage                                 |
| ------------------ | --------- | ------------------------------------- |
| `--bg-primary`     | `#FFFFFF` | Page background                       |
| `--bg-secondary`   | `#F8FAFC` | Card backgrounds                      |
| `--bg-tertiary`    | `#F1F5F9` | Elevated surfaces, input backgrounds  |
| `--text-primary`   | `#0F172A` | Primary text                          |
| `--text-secondary` | `#475569` | Secondary text                        |
| `--text-muted`     | `#94A3B8` | Muted text                            |
| `--accent-primary` | `#2563EB` | Primary actions (darker for contrast) |
| `--accent-success` | `#16A34A` | Success                               |
| `--accent-warning` | `#D97706` | Warning                               |
| `--accent-danger`  | `#DC2626` | Danger                                |
| `--accent-info`    | `#0891B2` | Info                                  |
| `--border`         | `#E2E8F0` | Borders                               |
| `--border-subtle`  | `#F1F5F9` | Subtle dividers                       |

### Semantic Colors (both modes)

| Token              | Dark      | Light     | Usage                     |
| ------------------ | --------- | --------- | ------------------------- |
| `--mode-present`   | `#22C55E` | `#16A34A` | USB Gadget Mode active    |
| `--mode-edit`      | `#F59E0B` | `#D97706` | Edit Mode active          |
| `--fsd-engaged`    | `#8B5CF6` | `#7C3AED` | FSD engaged route segment |
| `--fsd-disengaged` | `#F97316` | `#EA580C` | Manual driving segment    |
| `--sentry-event`   | `#EF4444` | `#DC2626` | Sentry clip markers       |
| `--saved-event`    | `#3B82F6` | `#2563EB` | Saved clip markers        |
| `--recent-event`   | `#22C55E` | `#16A34A` | Recent clip markers       |

### Rules

- Always test both light and dark modes before merging
- Text contrast must meet WCAG AA minimum (4.5:1 normal text, 3:1 large text)
- In dark mode, use border glow (`box-shadow`) instead of drop shadows
- Video player and map overlays always use dark background regardless of theme

---

## Typography

### Font Stack

| Role              | Font           | Weight         | Fallback              |
| ----------------- | -------------- | -------------- | --------------------- |
| Headings          | Inter          | 600 (semibold) | system-ui, sans-serif |
| Body              | Inter          | 400 (regular)  | system-ui, sans-serif |
| Labels/Captions   | Inter          | 500 (medium)   | system-ui, sans-serif |
| Monospace (stats) | JetBrains Mono | 400            | monospace             |

Inter is bundled as a local WOFF2 variable font (~95KB). Do not load fonts from external CDNs — the device may be offline on the AP network.

### Type Scale

```css
--text-xs: 0.75rem; /* 12px — captions, badges */
--text-sm: 0.875rem; /* 14px — secondary text, table cells */
--text-base: 1rem; /* 16px — body text (minimum for mobile) */
--text-lg: 1.125rem; /* 18px — card titles, emphasized text */
--text-xl: 1.25rem; /* 20px — section headings */
--text-2xl: 1.5rem; /* 24px — page titles */
--text-3xl: 1.875rem; /* 30px — hero numbers (analytics) */
```

### Rules

- Body text minimum: `16px` on mobile (never smaller)
- Line height: `1.5` for body text, `1.25` for headings
- Line length: max `65–75 characters` for readable body text
- Do not use `font-weight: bold` (700) — use `600` (semibold) for emphasis

---

## Icons

### Lucide Icons (SVG)

All icons use [Lucide](https://lucide.dev/) delivered as an inline SVG sprite. Do not use icon fonts or external CDNs.

**Never use emojis as UI icons.** Emojis render differently across devices and are not accessible.

| Context        | Icon Name                        | Notes             |
| -------------- | -------------------------------- | ----------------- |
| Map/trips      | `map-pin`                        |                   |
| Videos         | `video`                          |                   |
| Lock chimes    | `bell`                           |                   |
| Music          | `music`                          |                   |
| Light shows    | `sparkles`                       |                   |
| Wraps          | `palette`                        |                   |
| License plates | `credit-card`                    |                   |
| Settings       | `settings`                       |                   |
| Analytics      | `bar-chart-2`                    |                   |
| Mode toggle    | `refresh-cw`                     |                   |
| Upload         | `upload`                         |                   |
| Download       | `download`                       |                   |
| Delete         | `trash-2`                        |                   |
| Play           | `play`                           |                   |
| Edit           | `pencil`                         |                   |
| Close          | `x`                              |                   |
| Menu           | `menu`                           | Hamburger         |
| Sun            | `sun`                            | Light mode toggle |
| Moon           | `moon`                           | Dark mode toggle  |
| Chevron        | `chevron-left` / `chevron-right` | Navigation arrows |

### Rules

- Consistent size: `24×24` (`w-6 h-6`) for navigation, `20×20` (`w-5 h-5`) for inline, `16×16` (`w-4 h-4`) for badges
- Icon-only buttons must have `aria-label` for accessibility
- Use `currentColor` for icon fill/stroke so they inherit text color and respond to theme changes

---

## Spacing

```css
--space-1: 0.25rem; /*  4px */
--space-2: 0.5rem; /*  8px */
--space-3: 0.75rem; /* 12px */
--space-4: 1rem; /* 16px */
--space-5: 1.25rem; /* 20px */
--space-6: 1.5rem; /* 24px */
--space-8: 2rem; /* 32px */
--space-10: 2.5rem; /* 40px */
--space-12: 3rem; /* 48px */
--space-16: 4rem; /* 64px */
```

Use spacing tokens consistently. Do not use arbitrary pixel values.

---

## Border Radius

```css
--radius-sm: 0.25rem; /*  4px — badges, small elements */
--radius-md: 0.5rem; /*  8px — buttons, inputs */
--radius-lg: 0.75rem; /* 12px — cards */
--radius-xl: 1rem; /* 16px — modals, large cards */
--radius-full: 9999px; /*        pills, avatars */
```

---

## Shadows

```css
/* Light mode */
--shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.05);
--shadow-md: 0 4px 6px rgba(0, 0, 0, 0.07);
--shadow-lg: 0 10px 15px rgba(0, 0, 0, 0.1);

/* Dark mode — use subtle border glow instead */
--shadow-sm: 0 0 0 1px rgba(255, 255, 255, 0.05);
--shadow-md: 0 0 0 1px rgba(255, 255, 255, 0.08);
--shadow-lg: 0 4px 12px rgba(0, 0, 0, 0.4);
```

---

## Transitions & Animation

```css
--transition-fast: 150ms ease;
--transition-normal: 200ms ease;
--transition-slow: 300ms ease;
```

### Rules

- All interactive elements must use `transition: all var(--transition-normal)`
- Only animate `transform` and `opacity` — never `width`, `height`, or `margin` (causes reflows)
- Respect `prefers-reduced-motion: reduce` — disable all animations and transitions
- No continuous/infinite animations except loading spinners
- Hover states must not cause layout shift (no `scale` transforms that push content)

---

## Components

### Buttons

| Variant   | Dark Mode                           | Light Mode                          | Usage               |
| --------- | ----------------------------------- | ----------------------------------- | ------------------- |
| Primary   | `bg-blue-500 text-white`            | `bg-blue-600 text-white`            | Main actions        |
| Secondary | `bg-slate-700 text-slate-200`       | `bg-slate-200 text-slate-700`       | Secondary actions   |
| Ghost     | `text-slate-400 hover:bg-slate-800` | `text-slate-600 hover:bg-slate-100` | Tertiary/cancel     |
| Danger    | `bg-red-500/10 text-red-400`        | `bg-red-50 text-red-600`            | Destructive actions |
| Icon-only | `bg-slate-800 text-slate-400`       | `bg-slate-100 text-slate-600`       | Toolbar buttons     |

**All buttons:**

- Minimum touch target: `44×44px`
- Border radius: `var(--radius-md)`
- Cursor: `pointer`
- Disabled state: `40% opacity`, `cursor: not-allowed`
- Loading state: disable button, show spinner, prevent double-submit

### Cards

```css
.card {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  transition: all var(--transition-normal);
}
.card:hover {
  border-color: var(--accent-primary);
  box-shadow: var(--shadow-md);
}
```

### Form Inputs

```css
.input {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
  color: var(--text-primary);
  min-height: 44px;
  font-size: var(--text-base); /* 16px — prevents iOS zoom */
  transition: border-color var(--transition-fast);
}
.input:focus {
  border-color: var(--accent-primary);
  outline: 2px solid var(--accent-primary);
  outline-offset: 2px;
}
```

### Status Indicator

The underlying Present/Edit mode distinction is an implementation detail — never expose these terms to users.

**Normal state (USB connected to Tesla):**

- Small green dot in the nav — no label needed, this is the default happy state

**Network sharing active (Samba/Edit mode):**

- Amber dot + `"Sharing"` label in the nav
- Persistent subtle banner: `"Network Sharing active — Tesla USB disconnected"` with a "Reconnect" button

**Error/unknown state:**

- Red dot + `"Disconnected"` label

**Rules:**

- Never show "Present Mode" or "Edit Mode" in the UI
- Write operations (delete, upload) auto-switch modes transparently via `quick_edit` — the user never needs to know
- The only user-facing mode concept is "Network File Sharing" (enables Samba, found in Settings → Advanced)
- When network sharing is enabled, clearly warn that the Tesla USB is disconnected

### Tables

- Desktop: Traditional table with header row, row hover highlight
- Mobile (<768px): Convert to card list — each row becomes a card with `label: value` pairs
- Always wrap tables in `overflow-x-auto` container

### Toast Notifications

- Position: top-right (desktop), top-center (mobile)
- Auto-dismiss: 5 seconds
- Color-coded: success (green), error (red), warning (amber), info (blue)
- Include dismiss button
- Use `aria-live="polite"` for screen reader announcements

---

## Layout & Navigation

### Information Architecture

```
Map (/)              — Primary landing page, full-viewport map
Analytics (/analytics/) — Storage dashboard, stats, charts
Media (/media/)      — Lock Chimes, Music, Light Shows, Wraps, License Plates (sub-tabs)
Settings (/settings/) — Mode control, WiFi, AP, system config
```

### Desktop (≥1024px): Left Sidebar Rail

- 48px wide icon-only rail, collapsed by default
- Expands to 200px on hover/click to show labels
- Icons: Map, Analytics, Media, Settings
- Mode indicator dot at bottom (green = present, amber = edit)

### Mobile (<1024px): Bottom Tab Bar

```css
.tab-bar {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  height: 56px;
  padding-bottom: env(safe-area-inset-bottom);
  background: var(--bg-secondary);
  border-top: 1px solid var(--border);
  display: flex;
  justify-content: space-around;
  align-items: center;
  z-index: 50;
}
```

- 4 tabs with icon + label
- Active tab highlighted with `--accent-primary`
- Safe area padding for notched phones

### Responsive Breakpoints

| Name      | Width  | Layout                       |
| --------- | ------ | ---------------------------- |
| Mobile S  | 320px  | Single column, bottom tabs   |
| Mobile L  | 375px  | Single column, bottom tabs   |
| Tablet    | 768px  | 2 columns, bottom tabs       |
| Desktop   | 1024px | Sidebar rail + content       |
| Desktop L | 1440px | Sidebar rail + wider content |

### Key Responsive Behaviors

1. **Nav**: Bottom tabs (<1024px) → Sidebar rail (≥1024px)
2. **Map trip card**: Full-width bottom sheet (<768px) → Floating card (≥768px)
3. **Videos panel**: Bottom sheet (<768px) → Slide-in right panel (≥768px)
4. **Analytics grid**: 1 col (<768px) → 2 col (768–1024px) → 4 col (≥1024px)
5. **Tables**: Card list (<768px) → Traditional table (≥768px)

---

## Dark/Light Mode

### Toggle Behavior

- Icon button in nav: sun (light) / moon (dark)
- Respects `prefers-color-scheme` on first visit
- Saves preference to `localStorage`
- Applied via `data-theme="dark"` on `<html>` element
- Set in `<head>` before first paint to prevent flash of wrong theme

### CSS Architecture

```css
:root {
  /* Light mode tokens (default) */
}

[data-theme="dark"] {
  /* Dark mode overrides */
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    /* System dark mode fallback */
  }
}
```

### Map Theming

- Dark mode: dark map tiles (e.g., CartoDB Dark Matter)
- Light mode: standard OpenStreetMap tiles
- Video overlay backdrop: always dark regardless of theme

---

## Accessibility

These are non-negotiable requirements, not suggestions.

1. **Color contrast**: WCAG AA minimum — 4.5:1 for normal text, 3:1 for large text
2. **Focus indicators**: `outline: 2px solid var(--accent-primary); outline-offset: 2px` on all interactive elements
3. **Keyboard navigation**: Full tab order matching visual order. Enter/Space to activate. Escape to close panels/modals.
4. **ARIA labels**: Every icon-only button must have `aria-label`. Map markers must have descriptive labels.
5. **Screen reader**: `aria-live` regions for toast notifications, mode changes, upload progress
6. **Reduced motion**: `@media (prefers-reduced-motion: reduce)` disables all animations/transitions
7. **Touch targets**: Minimum `44×44px` for all interactive elements
8. **Semantic HTML**: Use `<nav>`, `<main>`, `<section>`, `<article>`, proper heading hierarchy (`h1` > `h2` > `h3`)
9. **Form labels**: Every input must have an associated `<label>` with `for` attribute
10. **Color not sole indicator**: Never use color alone to convey meaning — pair with icons, text, or patterns

---

## Performance (Pi Zero 2W Constraints)

This runs on a Raspberry Pi Zero 2 W with 512MB RAM. Every byte and millisecond matters.

1. **Bundle fonts locally** — Inter WOFF2 (~95KB). No external CDN calls.
2. **SVG icon sprite** — single file, cached aggressively via service worker
3. **No JavaScript frameworks** — vanilla JS + Jinja2 templates only. No React, Vue, etc.
4. **CSS custom properties** — theme switching is pure CSS, zero JS DOM manipulation
5. **Inline critical CSS** — first-paint styles in `<head>`, rest loaded async
6. **Lazy load images** — `loading="lazy"` on thumbnails, intersection observer for off-screen content
7. **Service worker** — cache static assets (CSS, JS, fonts, icons, map tiles)
8. **Minimize reflows** — animate only `transform` and `opacity`
9. **One file at a time** — any background processing (thumbnails, indexing) processes sequentially
10. **No external dependencies at runtime** — everything must work when the device is offline on its own AP network

---

## File Conventions

| Path                               | Purpose                                     |
| ---------------------------------- | ------------------------------------------- |
| `scripts/web/static/css/style.css` | All component and page styles (single file) |
| `scripts/web/static/js/main.js`    | Global JS (nav, theme, toasts, utilities)   |
| `scripts/web/static/fonts/`        | Bundled Inter WOFF2                         |
| `scripts/web/static/icons/`        | Lucide SVG sprite                           |
| `scripts/web/static/vendor/`       | Third-party libs (Leaflet, Chart.js)        |
| `scripts/web/templates/base.html`  | Master template (nav, theme toggle, toasts) |
| `scripts/web/templates/*.html`     | Page templates extending base.html          |
| `scripts/web/blueprints/*.py`      | Flask route handlers                        |
| `scripts/web/services/*.py`        | Business logic (no Flask dependency)        |

### Rules

- One CSS file (`style.css`) — do not create per-page CSS files
- CSS custom properties for all design tokens — no hardcoded values in component styles
- Templates extend `base.html` and use `{% block content %}` / `{% block scripts %}`
- Feature-gated pages must have a `@bp.before_request` guard checking for the relevant `.img` file

---

## Checklist — Before Merging Any UI Change

- [ ] No emojis used as icons (use Lucide SVG)
- [ ] All clickable elements have `cursor: pointer`
- [ ] All icon-only buttons have `aria-label`
- [ ] Touch targets are ≥44×44px
- [ ] Hover states use smooth transitions (150–300ms)
- [ ] Works in both light and dark mode
- [ ] Tested at 375px (mobile) and 1024px+ (desktop)
- [ ] No horizontal scrollbar on mobile
- [ ] Form inputs have associated `<label>` elements
- [ ] Focus states are visible for keyboard navigation
- [ ] `prefers-reduced-motion` respected
- [ ] No external resource loading (fonts, icons, CDNs)
- [ ] Colors use CSS custom property tokens, not hardcoded hex
