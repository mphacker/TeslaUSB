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
});
