# TeslaUSB Feature Ideas & TODO List

## 🎯 High-Priority Features

### 1. Video Search & Filtering
- [ ] **Date/time range filtering** ("show me videos from last Tuesday")
- [ ] **Event type filtering** (Recent/Saved/Sentry)
- [ ] **Camera angle filtering** (show only front camera clips)
- [ ] **Size/duration filtering** (find clips longer than X minutes)
- [ ] **Full-text search** of filenames
- [ ] **Saved search presets**

**Impact:** Essential as video library grows  
**Difficulty:** Medium

---

### 2. Batch Video Operations
- [ ] **Select multiple videos** with checkboxes
- [ ] **Batch download as ZIP** archive
- [ ] **Batch move** between folders (Recent → Saved)
- [ ] **Batch export** with metadata (timestamps, GPS if available)
- [ ] **Create video compilation** (merge selected clips)

**Impact:** Huge time saver for managing many clips  
**Difficulty:** Medium-High

---

### 3. Video Metadata Viewer
- [ ] **Extract & display video metadata** (resolution, framerate, codec, bitrate)
- [ ] **Show recording duration** and file creation time
- [ ] **Display GPS coordinates** if embedded (useful for sentry events)
- [ ] **Event context** (was car in park/drive, charging status if available)

**Impact:** High - provides valuable context for clips  
**Difficulty:** Medium

---

### 4. Scheduled Mode Switching
- [ ] **Automatic mode transitions** based on schedule
- [ ] Example: Auto-switch to Edit mode at 2 AM for backups, return to Present mode at 4 AM
- [ ] **Day-of-week schedules** (Edit mode on weekends only)
- [ ] **Integration with external triggers** (switch when home WiFi detected)

**Impact:** Medium - automation and convenience  
**Difficulty:** Medium

---

### 5. Mobile-Optimized Interface
- [ ] **Responsive design improvements** for phone/tablet viewing
- [ ] **Touch-friendly controls** for video playback
- [ ] **Progressive Web App (PWA)** support for home screen installation
- [ ] **Mobile video player** optimizations (lower bandwidth streaming)

**Impact:** High - better mobile experience  
**Difficulty:** Medium

---

### 6. Notification System
- [ ] **Email/push notifications** when Sentry clips are recorded
- [ ] **Storage warnings** when space is low
- [ ] **Health alerts** (filesystem errors, mode switch failures)
- [ ] **Weekly summary reports** (X clips recorded, Y GB used)

**Impact:** High - proactive awareness  
**Difficulty:** Medium-High

---

### 7. Video Annotation & Notes
- [ ] **Add text notes** to specific videos ("This is the hit-and-run incident")
- [ ] **Star/favorite** important clips
- [ ] **Tag videos** with categories (parking lot dent, road trip, etc.)
- [ ] **Search by tags/notes**

**Impact:** High - organization and finding important clips  
**Difficulty:** Medium

---

### 8. Advanced Lock Chime Features
- [X] **Chime scheduler** (different chimes for different times/days)
- [ ] **Random chime rotation** (picks random chime from library)
- [X] **Seasonal chimes** (auto-switch holiday themes)
- [ ] **Preview multiple chimes** in quick succession
- [ ] **Volume normalization** (auto-adjust all chimes to consistent loudness)

**Impact:** Medium - enhanced customization  
**Difficulty:** Low-Medium

---

### 9. System Health Monitoring
- [ ] **Pi temperature monitoring** with alerts
- [ ] **Filesystem health checks** with proactive fsck scheduling
- [ ] **USB connection status** (is Tesla currently connected?)
- [ ] **Write cycle tracking** for SD card health
- [ ] **Service status dashboard** (all systemd services at a glance)

**Impact:** High - prevents problems before they occur  
**Difficulty:** Medium

---

### 10. Backup & Restore
- [ ] **One-click full backup** of all TeslaCam videos to network share
- [ ] **Incremental backup** (only new videos since last backup)
- [ ] **Restore from backup** functionality
- [ ] **Cloud backup integration** (Google Drive, Dropbox, S3)
- [ ] **Automatic backup scheduling**

**Impact:** High - data protection  
**Difficulty:** Medium-High

---

### 11 Video Clip Exporter
- [ ] **Create shareable clips** with specific start/end times
- [ ] **Add watermarks/timestamps** to exported videos
- [ ] **Format conversion** (Tesla MP4 → other formats)
- [ ] **Social media optimization** (Instagram/YouTube ready exports)

**Impact:** Medium - sharing and social media  
**Difficulty:** Medium

---

## 🌟 Quick Wins (Easy to Implement)

These features provide immediate value with minimal implementation effort:

1. [ ] **Dark mode toggle** - Easy CSS addition for night viewing
2. [ ] **Quick delete oldest** - One-click button to delete oldest 10 RecentClips
3. [ ] **Thumbnail size slider** - User-adjustable thumbnail dimensions
4. [ ] **Auto-refresh toggle** - Let users enable/disable auto-refresh on video page

---

## 💡 Most Impactful Features (Recommended Priority Order)

For maximum user value, prioritize in this order:

1. **Storage Analytics Dashboard** - Critical for managing limited SD card space
2. **Auto-Cleanup System** - Prevents "disk full" problems automatically
3. **Batch Video Operations** - Huge time saver for managing many clips
4. **Video Search & Filtering** - Essential as video library grows
5. **System Health Monitoring** - Prevents problems before they occur

---

## 📝 Implementation Notes

### Technical Considerations

- **Storage Analytics**: Can leverage existing `get_mount_path()` and partition iteration logic
- **Auto-Cleanup**: Should respect current mode (only run in Edit mode)
- **Batch Operations**: Requires frontend checkbox UI + backend ZIP creation with `zipfile` module
- **Notifications**: Could use `smtplib` for email, webhook for push notifications
- **PWA Support**: Add manifest.json and service worker for offline capability

### Dependencies to Consider

- **FFmpeg**: Already used for thumbnails, can be extended for video metadata extraction
- **Python Libraries**: 
  - `zipfile` for batch downloads
  - `schedule` for cron-like functionality
  - `psutil` for system monitoring
  - `gpxpy` for GPS data parsing (if available in video metadata)

### UI/UX Improvements

- Maintain mobile-first responsive design
- Add loading states for all async operations
- Progressive enhancement (features degrade gracefully)
- Accessibility (ARIA labels, keyboard navigation)

---

## 🔧 Current System Strengths to Build Upon

- Dual-mode architecture (Present/Edit) is solid foundation
- Template-based configuration system is flexible
- Existing thumbnail generation can be extended for previews
- Flask web framework allows easy API endpoints
- Systemd integration provides reliable service management

---

## 📅 Suggested Development Phases

### Phase 1: Foundation (Quick Wins)
- Disk usage meter
- Video count badges
- Dark mode toggle
- Last recording timestamp

### Phase 2: Core Features
- Storage Analytics Dashboard
- Video Search & Filtering
- System Health Monitoring

### Phase 3: Automation
- Auto-Cleanup System
- Scheduled Mode Switching
- Backup & Restore

### Phase 4: Advanced Features
- Batch Video Operations
- Video Annotation & Notes
- Notification System
- Advanced Lock Chime Features

---

*Last Updated: November 8, 2025*
