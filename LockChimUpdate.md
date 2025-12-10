# Lock Chime Audio Trimming Feature - Implementation Plan

## **Architecture Overview: Client-Heavy, Server-Light**

The key insight is to leverage the **Web Audio API** to do ALL heavy processing in the user's browser, sending only the final trimmed WAV file to the Pi.

---

## **Implementation Approach**

### **Phase 1: Browser-Side Processing (JavaScript)**
All resource-intensive work happens here, using the client's device CPU/RAM:

1. **File Upload Interception**
   - Intercept form submission with JavaScript
   - Read file as ArrayBuffer
   - File never hits server until trimmed

2. **Audio Decoding**
   - Use `AudioContext.decodeAudioData()` to decode any format (MP3, WAV, OGG, etc.)
   - Produces an `AudioBuffer` with raw PCM samples
   - Works natively in all modern browsers

3. **Waveform Visualization**
   - Draw on HTML5 Canvas with responsive sizing
     - Mobile: ~350×120px (fits phone screens)
     - Desktop: ~800×150px (optimal for desktop viewing)
   - Downsample audio for display: For 11-second audio, only need ~2000 peaks for smooth visualization
   - Use peak amplitude detection for classic waveform appearance
   - Update in real-time as user adjusts trim points
   - Canvas scales with viewport using CSS and dynamically adjusts resolution

4. **Trim Controls**
   - Two HTML5 range sliders (start/end time)
   - **Touch-optimized for mobile:** Larger touch targets, prevent accidental drags
   - Real-time file size calculation: `(duration / speed) × 44100 × 2 bytes × channels + 44 byte header`
   - **Smart constraint enforcement:** Automatically prevent sliders from creating >1MB file
   - Display: start time, end time, duration, estimated file size, effective duration (after speed adjustment)
   - **Visual feedback:** Red warning if approaching 1MB limit, green if within safe range
   - **Responsive layout:** Stack vertically on mobile, side-by-side on desktop

5. **Audio Preview**
   - Use `AudioBufferSourceNode` to play trimmed segment
   - No re-encoding needed for preview (Web Audio API plays from buffer)
   - Play/pause/stop controls

6. **Playback Speed Adjustment** (NEW)
   - Add speed control slider (0.5x to 2.0x, or custom range)
   - Auto-calculate optimal speed to fit Tesla time limits
   - Use `AudioBufferSourceNode.playbackRate` for preview
   - Apply speed change via `OfflineAudioContext` for final render
   - Speed changes preserve pitch (or optionally allow pitch shift)

7. **WAV Generation**
   - Extract trimmed portion from AudioBuffer
   - Apply speed adjustment if configured
   - Encode as PCM 16-bit WAV with proper headers (RIFF format)
   - Resample to 44.1kHz if source is different rate (using OfflineAudioContext)
   - Create Blob for upload

### **Phase 2: Server-Side Processing (Python/Pi)**
Minimal work on the resource-constrained Pi:

1. **Receive Pre-Trimmed WAV**
   - File is already trimmed, already PCM 16-bit, already 44.1kHz
   - Typically <1MB

2. **Optional FFmpeg Processing**
   - **ONLY IF** volume normalization is requested
   - Otherwise, skip FFmpeg entirely (huge CPU savings)
   - Just validate format and save directly

3. **File Operations**
   - Standard quick_edit_part2 flow for present mode
   - Atomic write with fsync
   - MD5 verification

---

## **File Structure & Components**

### **New Files:**

1. **`scripts/web/static/js/audio_trimmer.js`** (~400-500 lines)
   - `AudioTrimmer` class
   - Methods: `loadFile()`, `renderWaveform()`, `updateTrimRegion()`, `updatePlaybackSpeed()`, `playPreview()`, `exportWAV()`
   - Speed adjustment using OfflineAudioContext
   - Auto-calculate optimal speed to fit time constraints
   - WAV encoder function (convert Float32Array to Int16 WAV blob)
   - Size and duration calculation utilities

2. **`scripts/web/static/css/audio_trimmer.css`** (~100-150 lines)
   - **CRITICAL: Must match existing TeslaUSB app's look and feel**
   - Use existing CSS variables for colors, fonts, spacing (from `style.css`)
   - Trim editor modal/section styling consistent with other app sections
   - Waveform canvas styling (responsive sizing for mobile/desktop)
   - Slider styling (custom track colors for selected region, touch-friendly on mobile)
   - **Full responsive design matching existing app's mobile/desktop breakpoints**
   - Stack controls vertically on mobile, horizontal layout on desktop
   - Canvas sizing: Full width on mobile (~350px), larger on desktop (~800px)
   - Touch-optimized controls: Larger hit areas for sliders on mobile devices
   - Button styles must match existing `.edit-btn`, `.present-btn` classes
   - Icons consistent with current app (use existing icon library if applicable)

### **Modified Files:**

3. **`scripts/web/templates/lock_chimes.html`**
   - Add hidden trim editor section (collapsible like other sections in app)
   - Shows after file selection OR when editing existing chime
   - Add "Edit" button next to each chime in library (styled like existing action buttons)
   - **UI Integration:** Trim editor must blend seamlessly with existing lock chimes page
   - Use existing HTML structure patterns (cards, collapsible sections, info boxes)
   - Maintain consistent spacing, padding, and layout with rest of page
   - Replace direct upload with two-step process:
     - Step 1: Trim editor (with speed adjustment)
     - Step 2: Upload trimmed/adjusted result

4. **`scripts/web/blueprints/lock_chimes.py`**
   - Detect if upload is pre-trimmed (add `pre_trimmed` form field)
   - Add route for downloading existing chime for editing: `/lock_chimes/edit/<filename>`
   - Route trimmed files to simplified processing

5. **`scripts/web/services/lock_chime_service.py`**
   - New function: `save_pretrimmed_wav(file, filename, normalize, target_lufs)`
   - If `normalize=False`, skip ffmpeg entirely
   - If `normalize=True`, only run ffmpeg with volume filter

---

## **Resource Usage Comparison**

| Operation | Current (Server) | Proposed (Browser) | Pi CPU Savings |
|-----------|------------------|-------------------|----------------|
| Decode MP3/complex formats | FFmpeg on Pi | Web Audio API in browser | ~80% |
| Trim audio | FFmpeg with `-t` flag | JavaScript in browser | ~90% |
| Resample to 44.1kHz | FFmpeg | OfflineAudioContext | ~70% |
| Speed adjustment | FFmpeg atempo filter | OfflineAudioContext | ~85% |
| Generate waveform | N/A (new feature) | Canvas in browser | 100% (Pi does nothing) |
| Volume normalize (optional) | FFmpeg | FFmpeg (only when requested) | 0% (same) |

**Total CPU reduction:** ~65-75% for typical upload  
**RAM usage:** <5MB on Pi (vs potential 20-50MB for large file processing)

---

## **UI/UX Flow**

### **Upload New Chime Flow:**
```
User clicks "Choose File"
    ↓
File selected → JavaScript intercepts
    ↓
Show "Decoding audio..." spinner
    ↓
Web Audio API decodes → AudioBuffer
    ↓
Render waveform on canvas
Show trim editor with:
  - Waveform display
  - Start/end time sliders (default: full duration or first 10 sec if too long)
  - Playback speed slider (0.5x to 2.0x)
  - "Auto-fit Speed" button (calculates optimal speed for Tesla time limit)
  - Play button for preview (respects trim + speed)
  - Duration & file size display (updates in real-time)
  - "Upload" and "Cancel" buttons
    ↓
User adjusts sliders → waveform updates (selected region highlighted)
    ↓
User clicks "Preview" → plays trimmed segment at selected speed
    ↓
User satisfied → clicks "Upload Trimmed Audio"
    ↓
JavaScript creates WAV blob (PCM 16-bit 44.1kHz, trimmed, speed-adjusted)
    ↓
Upload blob to server with original filename
    ↓
Server validates, optionally normalizes, saves
    ↓
Success! File appears in library
```

### **Edit Existing Chime Flow:**
```
User clicks "Edit" button next to chime in library
    ↓
JavaScript loads the WAV file from server
    ↓
Show "Decoding audio..." spinner
    ↓
Web Audio API decodes → AudioBuffer
    ↓
Render waveform on canvas
Show trim editor (same as upload flow)
  - Pre-populated with current audio
  - User can trim, adjust speed, preview
    ↓
User clicks "Save Changes"
    ↓
JavaScript creates updated WAV blob
    ↓
Upload to server (replaces existing file)
    ↓
Success! Updated file in library
```

---

## **Playback Speed Feature Details**

### **Why Speed Adjustment?**
When a lock chime is under the 1MB file size limit but exceeds Tesla's time duration limit (varies by model, typically ~5-10 seconds), speed adjustment allows the user to fit the audio within the constraint without trimming content.

### **Implementation Approach:**

1. **Speed Range:**
   - Allow 0.5x (half speed) to 2.0x (double speed)
   - Use slider with fine increments (0.05x steps)
   - Display current speed multiplier and resulting duration

2. **Auto-Calculate Optimal Speed:**
   - Button: "Auto-fit to Tesla Limits" (fits both time AND file size)
   - **Intelligent Auto-Fit Algorithm:**
     ```javascript
     // Tesla limits (configurable)
     const MAX_DURATION = 10.0;  // seconds
     const MAX_FILE_SIZE = 1048576;  // 1MB in bytes
     
     function autoFitAudio(audioBuffer, startTime, endTime, currentSpeed) {
         let duration = endTime - startTime;
         let speed = currentSpeed;
         
         // Calculate current file size
         const channels = audioBuffer.numberOfChannels;
         let fileSize = (duration / speed) * 44100 * 2 * channels + 44;
         
         // Priority 1: Fit within file size limit
         if (fileSize > MAX_FILE_SIZE) {
             // Calculate minimum speed needed for 1MB
             const maxDuration = (MAX_FILE_SIZE - 44) / (44100 * 2 * channels);
             const requiredSpeed = duration / maxDuration;
             speed = Math.min(2.0, Math.max(0.5, requiredSpeed));
             
             // If even 2x speed can't fit in 1MB, must trim
             if (speed === 2.0) {
                 fileSize = (duration / 2.0) * 44100 * 2 * channels + 44;
                 if (fileSize > MAX_FILE_SIZE) {
                     // Calculate max trim duration that fits at 2x speed
                     const maxTrimDuration = ((MAX_FILE_SIZE - 44) / (44100 * 2 * channels)) * 2.0;
                     endTime = startTime + maxTrimDuration;
                     duration = maxTrimDuration;
                     showWarning(`Audio trimmed to ${maxTrimDuration.toFixed(1)}s at 2x speed to fit 1MB limit`);
                 }
             }
         }
         
         // Priority 2: Fit within time duration limit (if not already exceeded by size constraint)
         const effectiveDuration = duration / speed;
         if (effectiveDuration > MAX_DURATION) {
             const requiredSpeed = duration / MAX_DURATION;
             speed = Math.min(2.0, Math.max(speed, requiredSpeed));
             
             // Re-check file size with new speed
             fileSize = (duration / speed) * 44100 * 2 * channels + 44;
             if (fileSize > MAX_FILE_SIZE) {
                 // Need to trim to satisfy both constraints
                 const maxDuration = Math.min(
                     MAX_DURATION * 2.0,  // Max at 2x speed for time
                     ((MAX_FILE_SIZE - 44) / (44100 * 2 * channels)) * 2.0  // Max at 2x for size
                 );
                 endTime = startTime + maxDuration;
                 duration = maxDuration;
                 speed = 2.0;
                 showWarning(`Audio optimized: trimmed to ${maxDuration.toFixed(1)}s at 2x speed`);
             }
         }
         
         return { endTime, speed, fileSize, effectiveDuration: duration / speed };
     }
     ```

3. **Technical Implementation (Web Audio API):**
   - **Preview:** Use `AudioBufferSourceNode.playbackRate.value = speed`
   - **Final Render:** Use `OfflineAudioContext` to render speed-adjusted audio
     ```javascript
     const offlineContext = new OfflineAudioContext(
         channels, 
         Math.floor(sampleCount / speed),  // Fewer samples at higher speed
         44100
     );
     const source = offlineContext.createBufferSource();
     source.buffer = audioBuffer;
     source.playbackRate.value = speed;
     source.connect(offlineContext.destination);
     source.start();
     
     const renderedBuffer = await offlineContext.startRendering();
     // renderedBuffer now contains speed-adjusted audio
     ```

4. **Pitch Preservation:**
   - Basic `playbackRate` changes pitch (chipmunk effect at 2x, deep at 0.5x)
   - **Option A:** Accept pitch change (simpler, no extra processing)
   - **Option B:** Use pitch-preserving algorithm (more complex)
     - Requires library like `soundtouchjs` or `rubberband.js`
     - Adds ~50KB and more CPU usage
     - **Recommendation:** Start with Option A (pitch shift), add Option B later if users request it

5. **UI Elements:**
   - Speed slider: 0.5x ← [1.0x] → 2.0x
   - Display: "Speed: 1.25x (Duration: 6.4s → 5.12s, Size: 564KB)"
   - Auto-fit button with tooltip: "Automatically optimize trim and speed to fit within 1MB and time limits"
   - Preview respects current speed setting
   - Visual indicators:
     - Speed ≠ 1.0x: Orange/yellow badge
     - File size > 900KB: Yellow warning
     - File size > 1MB: Red error (prevent upload)
     - Within all limits: Green checkmark

6. **File Size Considerations:**
   - Speed adjustment changes duration but maintains sample rate
   - File size formula: `(duration / speed) × 44100 × 2 bytes × channels + 44 bytes`
   - Faster playback = shorter file = smaller size
   - Real-time calculation prevents exceeding 1MB limit

### **Use Case Examples:**

**Example 1:** User uploads 12-second chime (800KB file)
- File size: OK (under 1MB)
- Duration: Too long (exceeds 10s limit)
- Click "Auto-fit" → System calculates 1.2x speed
- Result: 12s / 1.2 = 10s duration, ~667KB file
- User previews, accepts, uploads

**Example 2:** User has existing 15-second chime
- Clicks "Edit" button
- Trim editor loads
- Tries Auto-fit → 1.5x speed required
- User decides between:
  - Option A: Accept 1.5x speed (slightly faster/higher pitch)
  - Option B: Trim to 10s + use 1.0x speed (original pitch)
  - Option C: Trim to 13s + use 1.3x speed (compromise)

**Example 3:** User uploads 30-second stereo chime (2.5MB uncompressed)
- File size: Too large (exceeds 1MB)
- Duration: Way too long (exceeds 10s limit)
- Click "Auto-fit" → System:
  1. Calculates that even at 2x speed, file would be ~1.25MB
  2. Automatically trims to ~11.3 seconds
  3. Sets speed to 2x
  4. Result: 11.3s at 2x = 5.65s effective, ~998KB file
  5. Shows: "Audio optimized: trimmed to 11.3s at 2x speed to fit 1MB limit"
- User previews, accepts (or manually adjusts), uploads

**Example 4:** User uploads 8-second mono chime (950KB file)
- File size: Close to limit (90% of 1MB)
- Duration: OK (under 10s limit)
- User extends trim slider to include 2 more seconds
- System automatically prevents: "Cannot extend - would exceed 1MB limit"
- Suggests: "Try 1.1x speed to fit 10 seconds within 1MB" (auto-calculated)
- User clicks suggestion → 10s at 1.1x = 9.09s effective, 999KB file

---

## **Key Optimizations**

1. **Zero server processing for standard files:** If no normalization requested, server just validates and saves (no ffmpeg)

2. **Minimal memory footprint:** Browser releases original file after decode; Pi only holds 1MB temp file

3. **Progressive enhancement:** Feature detection for Web Audio API; graceful degradation to server-only processing for ancient browsers

4. **Smart defaults:** Auto-trim to first 10 seconds if file exceeds Tesla duration limit; suggest optimal speed if trimmed audio still too long

5. **Real-time validation:** Size and duration calculation prevents user from creating invalid files

5a. **Intelligent auto-fitting:** Single "Auto-fit" button optimizes both trim and speed to satisfy BOTH 1MB file size limit AND time duration limit, using minimal speed adjustment when possible

6. **Edit existing chimes:** Users can reload any chime from library to re-trim or adjust speed

7. **Mobile & Desktop Responsive:** Matches existing app's responsive design system
   - Breakpoints consistent with current TeslaUSB web app
   - Touch-optimized sliders and buttons (44×44px minimum)
   - Canvas auto-scales to viewport width
   - Controls stack vertically on mobile, horizontal on desktop
   - Tested on iOS Safari, Android Chrome, and desktop browsers

---

## **Libraries (Optional)**

- **Avoid wavesurfer.js** (100KB+, overkill)
- **Consider audiobuffer-to-wav** (2KB, handles WAV encoding) - OR write custom (50 lines)
- **Custom waveform rendering** (lightweight, tailored to needs)

---

## **Fallback Strategy**

For browsers without Web Audio API (basically IE11 and older):
- Hide trim editor
- Show standard upload form
- Server handles everything (current behavior)
- Detection: `if (!(window.AudioContext || window.webkitAudioContext)) { /* use fallback */ }`

---

## **Testing Considerations**

### **Format & Audio Testing:**
- Test with various formats: MP3, WAV, OGG, M4A
- Test with mono and stereo files
- Test with different sample rates: 22.05kHz, 44.1kHz, 48kHz, 96kHz
- Test edge case: trimming to <0.1 seconds
- Test large file: 10MB MP3 → trim to 500KB
- **Test speed adjustment:** 0.5x, 1.0x, 1.5x, 2.0x speeds
- **Test auto-fit speed:** Files at 12s, 15s, 20s durations
- **Test editing existing chimes:** Load, modify, save, verify changes
- **Test combined operations:** Trim + speed adjustment together
- **Test file size enforcement:** Upload files that would exceed 1MB, verify auto-fit works correctly
- **Test edge case:** Very large file (5MB+, 60s+) → verify intelligent trimming to fit 1MB
- **Test stereo vs mono:** Same duration, stereo is 2x file size, verify constraint handling

### **Responsive Design & Device Testing:**
- **Mobile devices:**
  - iOS: iPhone SE (320px), iPhone 12/13/14 (390px), iPhone Pro Max (428px)
  - Android: Various screen sizes from 360px to 412px wide
  - Test portrait and landscape orientations
  - Test touch interactions: drag sliders, tap buttons, pinch/zoom prevention
- **Tablets:**
  - iPad (768px), iPad Pro (1024px)
  - Test both portrait and landscape
- **Desktop:**
  - Small desktop (1024px), medium (1440px), large (1920px+)
  - Test mouse interactions vs touch on touchscreen laptops
- **Browsers:**
  - iOS Safari (primary mobile browser)
  - Android Chrome (primary Android browser)
  - Desktop: Chrome, Firefox, Edge, Safari
- **Responsive breakpoints:**
  - Verify trim editor layout matches existing app breakpoints
  - Canvas resizes appropriately without pixelation
  - Buttons and controls remain usable at all sizes
  - Text remains readable (minimum 14px on mobile)
- **Visual consistency testing:**
  - Compare trim editor appearance with existing lock chimes page
  - Verify colors match app theme (light/dark mode if applicable)
  - Check that buttons match existing button styles
  - Ensure spacing/padding consistent with rest of app
  - Validate icons match existing icon set
- **Navigation testing:**
  - Test "Cancel" returns to chime list without changes
  - Test "Back" buttons work intuitively
  - Verify upload progress doesn't block navigation if needed
  - Test that users can easily understand workflow (upload → trim → save)

---

## **Edge Cases and Error Handling**

1. **Very short files** (<0.5 seconds): Allow but show warning
2. **Mono vs Stereo**: Handle both, Tesla accepts both
3. **Different sample rates**: Browser can decode any rate, we resample to 44.1kHz in client
4. **File size prediction**: Calculate in real-time as sliders move
   - Formula: `(endTime - startTime) / speed × 44100 × 2 bytes × numChannels + WAV header (44 bytes)`
   - Show red warning if exceeds 1MB, prevent upload
5. **Browser compatibility**: Safari, Chrome, Firefox all support Web Audio API
6. **Mobile devices**: Touch-friendly sliders with larger hit areas (minimum 44×44px touch targets per accessibility guidelines), responsive canvas that adapts to screen width
7. **Corrupted audio**: Wrap AudioContext.decodeAudioData in try-catch
8. **Upload timeout**: For large trimmed files over slow connection, increase timeout
9. **Speed range limits**: Prevent speeds outside 0.5x-2.0x range
10. **Auto-fit impossible cases**: If audio can't fit within 1MB even at 2.0x speed, automatically apply intelligent trimming to satisfy constraint
11. **File size enforcement**: Prevent upload button from being enabled if file size > 1MB after all adjustments
12. **Real-time constraint checking**: As user adjusts sliders, continuously validate against BOTH file size and duration limits
13. **Editing conflicts**: Prevent editing same file from multiple browser tabs (optional)
14. **Undo/Reset**: Provide button to reset to original uploaded/loaded audio

---

## **UX Enhancements**

### **Design Consistency & Navigation:**
- **Visual consistency:** Use existing app's color scheme, fonts, and UI patterns
- **Seamless integration:** Trim editor appears as natural extension of lock chimes page
- **Clear navigation:** Obvious "Cancel" and "Back" options to return to main chime list
- **Progress indicators:** Match existing app's spinner/loader styles
- **Error messages:** Use existing alert/info box styles (success = green, warning = yellow, error = red)
- **Button styling:** Consistent with existing `.edit-btn`, `.present-btn`, etc.
- **Collapsible sections:** Use same expand/collapse behavior as other app sections

### **User Workflow Enhancements:**
- Auto-trim to first 10 seconds by default if file > Tesla duration limit
- Auto-calculate optimal speed when "Auto-fit" clicked
- Keyboard shortcuts: Space = play/pause, Left/Right arrows = nudge trim points, +/- = adjust speed
- Visual feedback during processing: "Decoding...", "Generating waveform...", "Uploading..."
- Color-coded regions: Blue/green for selected, gray for excluded
- Speed indicator: Orange waveform or badge when speed ≠ 1.0x
- Real-time preview of combined trim + speed effects
- "Reset" button to restore original audio state
- Tooltip hints: "Try Auto-fit Speed to make this chime fit Tesla's time limit"
- Zoom controls for precise trimming (optional future enhancement)
- **Edit mode badge:** When editing existing chime, show "Editing: ChimeName.wav" header
- **Intuitive controls:** Labels and instructions use same language/terminology as rest of app
- **Help text:** Brief, clear explanations matching existing app's help style

---

## **Configuration & Constraints**

### **Tesla Lock Chime Limits** (configurable in `config.py`):
```python
MAX_LOCK_CHIME_SIZE = 1024 * 1024  # 1 MB
MAX_LOCK_CHIME_DURATION = 10.0     # 10 seconds (configurable per Tesla model)
MIN_LOCK_CHIME_DURATION = 0.3      # 300ms minimum (configurable)
SPEED_RANGE_MIN = 0.5              # Half speed
SPEED_RANGE_MAX = 2.0              # Double speed
SPEED_STEP = 0.05                  # Fine-grained control
```

### **Validation Rules:**
1. **Final file size must be ≤ 1MB** (CRITICAL - enforced before upload allowed)
2. **Effective duration** (after speed adjustment) must be ≤ configured max (default 10s)
3. **Effective duration** must be ≥ configured min (default 0.3s)
4. Speed must be within 0.5x - 2.0x range
5. Format must be PCM 16-bit, 44.1kHz, mono or stereo
6. **Auto-fit priority:** File size constraint takes precedence over time constraint
   - If both can't be satisfied at 2x speed, trim is auto-applied
   - User is always notified when auto-trimming occurs

---

## **Implementation Phases**

### **Phase 1: Core Trimming Feature** (MVP)
- Audio upload interception
- Web Audio API decoder
- Waveform visualization
- Trim sliders (start/end)
- Preview playback
- WAV export and upload
- Server-side simplified processing
- **Estimated effort:** 2-3 days

### **Phase 2: Speed Adjustment** (Enhancement)
- Speed slider control
- Auto-fit speed calculator
- Speed-adjusted preview
- OfflineAudioContext rendering with playback rate
- Updated file size/duration calculations
- **Estimated effort:** 1-2 days

### **Phase 3: Edit Existing Chimes** (Enhancement)
- "Edit" button on each chime in library
- Route to serve existing WAV files
- Load existing file into trim editor
- Save/replace functionality
- Conflict prevention (optional)
- **Estimated effort:** 1 day

### **Phase 4: Polish & UX** (Optional)
- **Design consistency review:** Ensure trim editor matches existing app aesthetics
- Keyboard shortcuts
- Undo/reset functionality
- Advanced tooltips and help text
- Pitch-preserving speed adjustment (requires library)
- Zoom controls for precise editing
- Visual themes for trim editor (if app supports theming)
- **User testing:** Validate ease of navigation and intuitive workflow
- **Estimated effort:** 1-2 days

**Total estimated effort:** 5-8 days for full feature set

---

## **UI/UX Design Requirements Summary**

### **Visual Consistency:**
- Use existing CSS variables and color scheme from `scripts/web/static/css/style.css`
- Match font families, sizes, and weights used throughout the app
- Buttons must use existing button classes (`.edit-btn`, `.present-btn`, etc.)
- Icons should come from the same icon library used in the app
- Spacing and padding must align with existing app sections
- Support both light and dark themes if app has theme toggle

### **Structural Consistency:**
- Trim editor should appear as a collapsible section (like existing "Upload Controls" or "Chime Scheduler")
- Use same card/box styling as other content areas
- Alert messages (success/warning/error) must use existing info box styles
- Progress indicators match existing upload progress bar design
- Form inputs and sliders consistent with current form styling

### **Navigation & Usability:**
- Clear "Cancel" button returns to chime list without saving
- "Upload" or "Save" button clearly indicates final action
- Workflow is intuitive: File Select → Edit → Preview → Upload
- No more than 2-3 clicks to complete any action
- User always knows current state (uploading, editing, previewing)
- Error messages guide user to fix issues, not just report problems
- Mobile users can complete entire workflow one-handed if needed

### **Performance & Responsiveness:**
- Trim editor loads without blocking main page
- Waveform renders in <2 seconds for typical files
- UI remains responsive during audio processing
- Preview playback starts immediately (no lag)
- Real-time updates don't cause UI jank or stutter

---

## **Technical Constraints Summary**

### **Browser Requirements:**
- Modern browser with Web Audio API support (Chrome 35+, Firefox 25+, Safari 14.1+, Edge 79+)
- JavaScript enabled
- Sufficient RAM to decode audio (typically 10-50MB for working buffers)
- **Mobile browsers:** iOS Safari 14.1+, Android Chrome 90+
- **Responsive design:** Works on phone screens (320px+), tablets (768px+), and desktop (1024px+)

### **Raspberry Pi Requirements:**
- No additional packages needed beyond existing ffmpeg
- Minimal CPU impact (only for normalization if requested)
- Minimal RAM impact (<5MB per upload)
- No disk space increase (files already max 1MB)

### **Network Considerations:**
- Upload size reduced (only trimmed audio sent)
- For editing: Download existing chime (~100KB-1MB), then upload modified version
- Total bandwidth for edit operation: 2-3MB worst case (download + upload)

---

This approach **maximizes browser capabilities** (which can be very powerful on modern smartphones/laptops) while **minimizing load on the constrained Pi Zero 2 W**. The Pi essentially becomes a simple file server for pre-processed audio.
