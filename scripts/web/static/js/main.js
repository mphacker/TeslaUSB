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

// Auto-close mobile menu when navigation links are clicked
document.addEventListener('DOMContentLoaded', function() {
    const mobileMenu = document.getElementById('mobileMenu');
    const overlay = document.getElementById('mobileMenuOverlay');
    if (mobileMenu && overlay) {
        // Find all navigation links in the mobile menu (excluding theme toggle button)
        const navLinks = mobileMenu.querySelectorAll('a[href]');
        navLinks.forEach(function(link) {
            link.addEventListener('click', function() {
                // Force-remove active classes immediately (don't toggle, just remove)
                mobileMenu.classList.remove('active');
                overlay.classList.remove('active');
            });
        });
    }
});

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

// ============================================================================
// Chime Groups Management
// ============================================================================

document.addEventListener('DOMContentLoaded', function() {
    const groupModal = document.getElementById('groupModal');
    const createGroupBtn = document.getElementById('createGroupBtn');
    const groupModalClose = document.getElementById('groupModalClose');
    const cancelGroupBtn = document.getElementById('cancelGroupBtn');
    const groupForm = document.getElementById('groupForm');
    const randomGroupSelect = document.getElementById('randomGroupSelect');
    const toggleRandomMode = document.getElementById('toggleRandomMode');

    if (!groupModal) return; // Not on lock chimes page

    // Open modal for creating new group
    if (createGroupBtn) {
        createGroupBtn.addEventListener('click', function() {
            const modalTitle = document.getElementById('groupModalTitle');
            const groupFormId = document.getElementById('groupFormId');
            const groupName = document.getElementById('groupName');
            const groupDescription = document.getElementById('groupDescription');

            if (modalTitle) modalTitle.textContent = 'Create New Group';
            if (groupFormId) groupFormId.value = '';
            if (groupName) groupName.value = '';
            if (groupDescription) groupDescription.value = '';

            // Uncheck all chimes
            document.querySelectorAll('#chimeCheckboxes input[type="checkbox"]').forEach(cb => {
                cb.checked = false;
            });

            groupModal.classList.add('show');
        });
    }

    // Close modal
    function closeGroupModal() {
        groupModal.classList.remove('show');
    }

    if (groupModalClose) {
        groupModalClose.addEventListener('click', closeGroupModal);
    }

    if (cancelGroupBtn) {
        cancelGroupBtn.addEventListener('click', closeGroupModal);
    }

    // Close modal when clicking outside
    groupModal.addEventListener('click', function(e) {
        if (e.target === groupModal) {
            closeGroupModal();
        }
    });

    // Handle group form submission
    if (groupForm) {
        groupForm.addEventListener('submit', async function(e) {
            e.preventDefault();

            const groupId = document.getElementById('groupFormId').value;
            const groupName = document.getElementById('groupName').value.trim();
            const groupDescription = document.getElementById('groupDescription').value.trim();

            // Collect selected chimes
            const selectedChimes = [];
            document.querySelectorAll('#chimeCheckboxes input[type="checkbox"]:checked').forEach(cb => {
                selectedChimes.push(cb.value);
            });

            if (!groupName) {
                alert('Please enter a group name');
                return;
            }

            try {
                let url, method, data;

                if (groupId) {
                    // Update existing group
                    url = `/lock_chimes/groups/${groupId}/update`;
                    method = 'POST';
                    data = {
                        name: groupName,
                        description: groupDescription
                    };

                    // Note: For simplicity, we'll handle chime updates separately
                    // in the edit functionality. This just updates name/description.
                } else {
                    // Create new group
                    url = '/lock_chimes/groups/create';
                    method = 'POST';
                    data = {
                        name: groupName,
                        description: groupDescription
                    };
                }

                const response = await fetch(url, {
                    method: method,
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify(data)
                });

                const result = await response.json();

                if (result.success) {
                    // If creating a new group, add chimes to it
                    if (!groupId && selectedChimes.length > 0) {
                        const newGroupId = result.group_id;
                        for (const chime of selectedChimes) {
                            await fetch(`/lock_chimes/groups/${newGroupId}/add_chime`, {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json'
                                },
                                body: JSON.stringify({ chime_filename: chime })
                            });
                        }
                    }

                    alert(result.message || 'Group saved successfully');
                    location.reload();
                } else {
                    alert('Error: ' + (result.error || 'Unknown error'));
                }
            } catch (error) {
                console.error('Error saving group:', error);
                alert('Failed to save group: ' + error.message);
            }
        });
    }

    // Handle edit group buttons
    document.querySelectorAll('.edit-group-btn').forEach(btn => {
        btn.addEventListener('click', async function() {
            const groupId = this.dataset.groupId;

            try {
                const response = await fetch(`/lock_chimes/groups/list`);
                const result = await response.json();

                if (result.success) {
                    const group = result.groups.find(g => g.id === groupId);
                    if (group) {
                        document.getElementById('groupModalTitle').textContent = 'Edit Group';
                        document.getElementById('groupFormId').value = groupId;
                        document.getElementById('groupName').value = group.name;
                        document.getElementById('groupDescription').value = group.description || '';

                        // Check chimes that are in this group
                        document.querySelectorAll('#chimeCheckboxes input[type="checkbox"]').forEach(cb => {
                            cb.checked = group.chimes.includes(cb.value);
                        });

                        groupModal.classList.add('show');
                    }
                }
            } catch (error) {
                console.error('Error loading group:', error);
                alert('Failed to load group details');
            }
        });
    });

    // Handle delete group buttons
    document.querySelectorAll('.delete-group-btn').forEach(btn => {
        btn.addEventListener('click', async function() {
            const groupId = this.dataset.groupId;
            const groupCard = this.closest('.group-card');
            const groupName = groupCard.querySelector('.group-name').textContent.trim();

            if (!confirm(`Are you sure you want to delete the group "${groupName}"?`)) {
                return;
            }

            try {
                const response = await fetch(`/lock_chimes/groups/${groupId}/delete`, {
                    method: 'POST'
                });

                const result = await response.json();

                if (result.success) {
                    alert(result.message || 'Group deleted successfully');
                    location.reload();
                } else {
                    alert('Error: ' + (result.error || 'Unknown error'));
                }
            } catch (error) {
                console.error('Error deleting group:', error);
                alert('Failed to delete group: ' + error.message);
            }
        });
    });

    // Handle remove chime from group
    document.querySelectorAll('.chime-tag-remove').forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.stopPropagation();
            const groupId = this.dataset.groupId;
            const chime = this.dataset.chime;

            if (!confirm(`Remove "${chime}" from this group?`)) {
                return;
            }

            try {
                const response = await fetch(`/lock_chimes/groups/${groupId}/remove_chime`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ chime_filename: chime })
                });

                const result = await response.json();

                if (result.success) {
                    // Remove the tag from UI
                    this.closest('.chime-tag').remove();
                } else {
                    alert('Error: ' + (result.error || 'Unknown error'));
                }
            } catch (error) {
                console.error('Error removing chime:', error);
                alert('Failed to remove chime: ' + error.message);
            }
        });
    });

    // Handle random mode toggle
    if (toggleRandomMode) {
        toggleRandomMode.addEventListener('click', async function() {
            const isCurrentlyEnabled = this.textContent.includes('✓ Enabled');
            const selectedGroupId = randomGroupSelect.value;

            if (!isCurrentlyEnabled && !selectedGroupId) {
                alert('Please select a group first');
                return;
            }

            try {
                const response = await fetch('/lock_chimes/groups/random_mode', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        enabled: !isCurrentlyEnabled,
                        group_id: selectedGroupId
                    })
                });

                const result = await response.json();

                if (result.success) {
                    alert(result.message || 'Random mode updated');
                    location.reload();
                } else {
                    alert('Error: ' + (result.error || 'Unknown error'));
                }
            } catch (error) {
                console.error('Error toggling random mode:', error);
                alert('Failed to update random mode: ' + error.message);
            }
        });
    }

    // Update random mode button when group selection changes
    if (randomGroupSelect) {
        randomGroupSelect.addEventListener('change', function() {
            if (toggleRandomMode) {
                const isEnabled = toggleRandomMode.textContent.includes('✓ Enabled');
                if (isEnabled) {
                    toggleRandomMode.textContent = 'Update';
                } else {
                    toggleRandomMode.textContent = 'Enable';
                }
            }
        });
    }
});


