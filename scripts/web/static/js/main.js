// TeslaUSB Web Interface JavaScript

// Theme Toggle Functionality
function initTheme() {
    // Check for saved theme preference or default to system preference
    const savedTheme = localStorage.getItem('theme');
    const systemPrefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    
    let theme;
    if (savedTheme) {
        theme = savedTheme;
    } else if (systemPrefersDark) {
        theme = 'dark';
    } else {
        theme = 'light';
    }
    
    applyTheme(theme);
    
    // Listen for system theme changes
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
        // Only auto-switch if user hasn't set a preference
        if (!localStorage.getItem('theme')) {
            applyTheme(e.matches ? 'dark' : 'light');
        }
    });
}

function applyTheme(theme) {
    if (theme === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
        updateThemeIcon('sun');
    } else {
        document.documentElement.removeAttribute('data-theme');
        updateThemeIcon('moon');
    }
}

function updateThemeIcon(iconType) {
    const iconDesktop = document.getElementById('theme-icon');
    const iconMobile = document.getElementById('theme-icon-mobile');
    const label = document.getElementById('theme-label');
    
    if (iconType === 'sun') {
        // Dark mode active - show sun icon
        if (iconDesktop) {
            iconDesktop.className = 'bi bi-sun-fill';
        }
        if (iconMobile) {
            iconMobile.className = 'bi bi-sun-fill';
        }
        if (label) {
            label.textContent = 'Light Mode';
        }
    } else {
        // Light mode active - show moon icon
        if (iconDesktop) {
            iconDesktop.className = 'bi bi-moon-stars-fill';
        }
        if (iconMobile) {
            iconMobile.className = 'bi bi-moon-stars-fill';
        }
        if (label) {
            label.textContent = 'Dark Mode';
        }
    }
}

function toggleTheme() {
    const currentTheme = document.documentElement.getAttribute('data-theme');
    const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
    
    // Save user preference
    localStorage.setItem('theme', newTheme);
    
    // Apply new theme
    applyTheme(newTheme);
}

// Initialize theme on page load
initTheme();

// Mobile menu toggle
function toggleMobileMenu() {
    const mobileMenu = document.getElementById('mobileMenu');
    const overlay = document.getElementById('mobileMenuOverlay');
    
    if (mobileMenu && overlay) {
        mobileMenu.classList.toggle('active');
        overlay.classList.toggle('active');
    }
}

// Global audio player management: pause all other audio/video when one starts playing
// (Excluded from multi-camera session view where multiple videos play simultaneously)
document.addEventListener('DOMContentLoaded', function() {
    // Check if we're on the multi-camera session view page
    const isSessionView = document.querySelector('.session-grid') !== null;
    
    // Skip auto-pause behavior on session view (has its own sync controls)
    if (isSessionView) {
        return;
    }
    
    // Get all audio and video elements on the page
    const allMediaElements = document.querySelectorAll('audio, video');
    
    allMediaElements.forEach(function(media) {
        media.addEventListener('play', function() {
            // When this media starts playing, pause all others
            allMediaElements.forEach(function(otherMedia) {
                if (otherMedia !== media && !otherMedia.paused) {
                    otherMedia.pause();
                }
            });
        });
    });
    
    // Operation in progress auto-refresh polling
    if (window.operationInProgress) {
        initOperationPolling();
    }
});

// Operation polling for auto-refresh
let pollCount = 0;
const MAX_POLLS = 20; // Max 60 seconds (20 polls × 3s)

function initOperationPolling() {
    pollCount = 0;
    setTimeout(checkOperationStatus, 3000); // Start polling after 3 seconds
}

function checkOperationStatus() {
    pollCount++;
    
    fetch('/api/operation_status')
        .then(response => response.json())
        .then(data => {
            if (data.in_progress) {
                // Operation still in progress
                updateCountdownMessage(pollCount);
                
                if (pollCount < MAX_POLLS) {
                    // Continue polling
                    setTimeout(checkOperationStatus, 3000);
                } else {
                    // Max retries reached
                    showMaxRetryMessage();
                }
            } else {
                // Operation complete - refresh page
                console.log('Operation completed, refreshing page...');
                location.reload();
            }
        })
        .catch(error => {
            console.error('Error checking operation status:', error);
            // Retry on error
            if (pollCount < MAX_POLLS) {
                setTimeout(checkOperationStatus, 3000);
            }
        });
}

function updateCountdownMessage(count) {
    const countdownEl = document.getElementById('retry-countdown');
    if (countdownEl) {
        const elapsed = count * 3;
        const remaining = Math.max(0, MAX_POLLS * 3 - elapsed);
        
        if (elapsed < 15) {
            countdownEl.textContent = `Checking again in 3s... (${Math.floor(remaining / 60)}m ${remaining % 60}s remaining)`;
        } else if (elapsed < 30) {
            countdownEl.textContent = `Still processing... Auto-refresh in 3s (${Math.floor(remaining / 60)}m ${remaining % 60}s)`;
        } else {
            countdownEl.textContent = `Operation taking longer than usual... Will keep checking (${Math.floor(remaining / 60)}m ${remaining % 60}s)`;
        }
    }
}

function showMaxRetryMessage() {
    const countdownEl = document.getElementById('retry-countdown');
    if (countdownEl) {
        countdownEl.innerHTML = '<strong>Operation is taking longer than expected. Please manually refresh the page.</strong>';
    }
}

// Volume Normalization Slider - Lock Chimes Page
document.addEventListener('DOMContentLoaded', function() {
    // Only run on lock chimes page
    const targetLufsSlider = document.getElementById('target-lufs');
    if (!targetLufsSlider) return;
    
    const currentLevelName = document.getElementById('current-level');
    const levelDescription = document.getElementById('level-description');
    const normalizeCheckbox = document.getElementById('normalize-on-upload');
    const normalizationSettings = document.getElementById('normalization-settings');
    
    // Volume presets configuration
    const VOLUME_PRESETS = [
        { index: 0, lufs: -23, name: 'Broadcast', description: 'Quietest - broadcast standard (EBU R128)' },
        { index: 1, lufs: -16, name: 'Streaming', description: 'Recommended for balanced playback' },
        { index: 2, lufs: -14, name: 'Loud', description: 'Louder streaming services (Apple Music)' },
        { index: 3, lufs: -12, name: 'Maximum', description: 'Maximum safe volume - may be very loud' }
    ];
    
    // Update display when slider changes
    function updateVolumeDisplay(index) {
        const preset = VOLUME_PRESETS[index];
        currentLevelName.textContent = preset.name;
        levelDescription.textContent = preset.description;
        
        // Change color based on level (green -> blue -> orange -> red)
        const colors = ['#4CAF50', '#2196F3', '#FF9800', '#F44336'];
        currentLevelName.style.color = colors[index];
    }
    
    // Get actual LUFS value from slider index
    function getLUFSValue() {
        const index = parseInt(targetLufsSlider.value);
        return VOLUME_PRESETS[index].lufs;
    }
    
    // Slider input event
    targetLufsSlider.addEventListener('input', function(e) {
        updateVolumeDisplay(parseInt(e.target.value));
    });
    
    // Show/hide settings based on checkbox
    normalizeCheckbox.addEventListener('change', function(e) {
        normalizationSettings.style.display = e.target.checked ? 'block' : 'none';
    });
    
    // Save preference to localStorage on change
    targetLufsSlider.addEventListener('change', function(e) {
        localStorage.setItem('preferredVolumeLevel', e.target.value);
    });
    
    // Save checkbox state to localStorage
    normalizeCheckbox.addEventListener('change', function(e) {
        localStorage.setItem('normalizeOnUpload', e.target.checked ? 'true' : 'false');
    });
    
    // Load saved preferences on page load
    const savedLevel = localStorage.getItem('preferredVolumeLevel');
    const savedNormalize = localStorage.getItem('normalizeOnUpload');
    
    if (savedLevel !== null) {
        targetLufsSlider.value = savedLevel;
        updateVolumeDisplay(parseInt(savedLevel));
    } else {
        // Default to Streaming (index 1)
        targetLufsSlider.value = 1;
        updateVolumeDisplay(1);
    }
    
    if (savedNormalize !== null) {
        normalizeCheckbox.checked = savedNormalize === 'true';
        normalizationSettings.style.display = normalizeCheckbox.checked ? 'block' : 'none';
    }
    
    // Intercept form submission to add LUFS value
    const uploadForm = document.getElementById('chimeUploadForm');
    if (uploadForm) {
        uploadForm.addEventListener('submit', function(e) {
            // Add hidden input with actual LUFS value
            let lufsInput = uploadForm.querySelector('input[name="target_lufs"]');
            if (!lufsInput) {
                lufsInput = document.createElement('input');
                lufsInput.type = 'hidden';
                lufsInput.name = 'target_lufs';
                uploadForm.appendChild(lufsInput);
            }
            lufsInput.value = getLUFSValue();
        });
    }
});

