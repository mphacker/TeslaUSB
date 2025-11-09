// TeslaUSB Web Interface JavaScript

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
