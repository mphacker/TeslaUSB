/**
 * TeslaUSB Audio Trimmer
 * 
 * Client-side audio trimming, waveform visualization, and speed adjustment
 * for lock chime files. Uses Web Audio API for all heavy processing.
 */

class AudioTrimmer {
    constructor(options = {}) {
        // Configuration
        this.maxFileSize = options.maxFileSize || 1048576; // 1MB
        this.maxDuration = options.maxDuration || 10.0; // 10 seconds
        this.minDuration = options.minDuration || 0.3; // 300ms
        this.speedMin = options.speedMin || 0.5;
        this.speedMax = options.speedMax || 2.0;
        this.speedStep = options.speedStep || 0.05;
        this.targetSampleRate = 44100;
        
        // State
        this.audioBuffer = null;
        this.audioContext = null;
        this.sourceNode = null;
        this.startTime = 0;
        this.endTime = 0;
        this.playbackSpeed = 1.0;
        this.isPlaying = false;
        this.originalFileName = '';
        this.editMode = false; // true when editing existing chime
        this.lastActualFileSize = null; // Cache for actual encoded file size
        
        // Track initial state to detect changes
        this.initialStartTime = 0;
        this.initialEndTime = 0;
        this.initialPlaybackSpeed = 1.0;
        
        // Volume normalization
        this.normalizeVolume = false;
        this.targetLUFS = -14; // Default target loudness
        this.gainMultiplier = 1.0; // Current gain applied
        
        // Canvas rendering
        this.canvas = null;
        this.canvasContext = null;
        this.waveformData = null;
        
        // Playback position tracking
        this.playbackStartTime = 0;
        this.playbackAnimationFrame = null;
        this.currentPlaybackPosition = 0;
        
        // Initialize Audio Context
        this.initAudioContext();
    }
    
    initAudioContext() {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) {
            throw new Error('Web Audio API not supported in this browser');
        }
        this.audioContext = new AudioContextClass();
    }
    
    /**
     * Load audio file from File object or URL
     */
    async loadFile(fileOrUrl, fileName = '') {
        try {
            let arrayBuffer;
            
            if (typeof fileOrUrl === 'string') {
                // Loading from URL (editing existing chime)
                this.editMode = true;
                const response = await fetch(fileOrUrl);
                if (!response.ok) {
                    throw new Error(`Failed to fetch audio: ${response.statusText}`);
                }
                arrayBuffer = await response.arrayBuffer();
                this.originalFileName = fileName || 'chime.wav';
            } else {
                // Loading from File object (new upload)
                this.editMode = false;
                arrayBuffer = await this.readFileAsArrayBuffer(fileOrUrl);
                this.originalFileName = fileOrUrl.name;
            }
            
            // Decode audio data
            this.audioBuffer = await this.audioContext.decodeAudioData(arrayBuffer);
            
            // Initialize trim points
            const duration = this.audioBuffer.duration;
            this.startTime = 0;
            
            // Auto-trim to max duration if file is too long
            if (duration > this.maxDuration) {
                this.endTime = Math.min(duration, this.maxDuration);
            } else {
                this.endTime = duration;
            }
            
            // Reset speed
            this.playbackSpeed = 1.0;
            
            // Store initial state
            this.initialStartTime = this.startTime;
            this.initialEndTime = this.endTime;
            this.initialPlaybackSpeed = this.playbackSpeed;
            
            // Generate waveform data
            this.generateWaveformData();
            
            return {
                success: true,
                duration: duration,
                channels: this.audioBuffer.numberOfChannels,
                sampleRate: this.audioBuffer.sampleRate
            };
            
        } catch (error) {
            console.error('Error loading audio file:', error);
            return {
                success: false,
                error: error.message
            };
        }
    }
    
    readFileAsArrayBuffer(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = (e) => resolve(e.target.result);
            reader.onerror = (e) => reject(new Error('Failed to read file'));
            reader.readAsArrayBuffer(file);
        });
    }
    
    /**
     * Generate downsampled waveform data for visualization
     */
    generateWaveformData() {
        const samples = this.audioBuffer.getChannelData(0); // Use first channel
        const targetPoints = 2000; // Number of peaks to display
        const blockSize = Math.floor(samples.length / targetPoints);
        
        this.waveformData = new Float32Array(targetPoints);
        
        for (let i = 0; i < targetPoints; i++) {
            let sum = 0;
            let max = 0;
            const offset = i * blockSize;
            
            for (let j = 0; j < blockSize; j++) {
                const val = Math.abs(samples[offset + j] || 0);
                sum += val;
                if (val > max) max = val;
            }
            
            // Use peak amplitude for classic waveform look
            this.waveformData[i] = max;
        }
    }
    
    /**
     * Render waveform on canvas
     */
    renderWaveform(canvas) {
        if (!this.audioBuffer || !this.waveformData) {
            return;
        }
        
        this.canvas = canvas;
        this.canvasContext = canvas.getContext('2d');
        
        const width = canvas.width;
        const height = canvas.height;
        const ctx = this.canvasContext;
        
        // Clear canvas
        ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--bg-secondary') || '#ffffff';
        ctx.fillRect(0, 0, width, height);
        
        // Calculate trim region in pixels based on ORIGINAL timeline
        // Waveform always shows original audio, speed only affects playback rate
        const duration = this.audioBuffer.duration;
        const startPixel = (this.startTime / duration) * width;
        const endPixel = (this.endTime / duration) * width;
        
        // Draw excluded regions (gray)
        ctx.fillStyle = 'rgba(128, 128, 128, 0.2)';
        ctx.fillRect(0, 0, startPixel, height);
        ctx.fillRect(endPixel, 0, width - endPixel, height);
        
        // Draw selected region background (light blue)
        ctx.fillStyle = 'rgba(33, 150, 243, 0.1)';
        ctx.fillRect(startPixel, 0, endPixel - startPixel, height);
        
        // Draw waveform
        const barWidth = width / this.waveformData.length;
        const halfHeight = height / 2;
        
        for (let i = 0; i < this.waveformData.length; i++) {
            const x = i * barWidth;
            const amplitude = this.waveformData[i];
            const barHeight = amplitude * halfHeight;
            
            // Color based on whether in selected region
            if (x >= startPixel && x <= endPixel) {
                ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--btn-primary-bg') || '#007bff';
            } else {
                ctx.fillStyle = 'rgba(128, 128, 128, 0.5)';
            }
            
            // Draw bar from center
            ctx.fillRect(x, halfHeight - barHeight, Math.max(barWidth, 1), barHeight * 2);
        }
        
        // Draw trim markers
        ctx.strokeStyle = '#f44336';
        ctx.lineWidth = 2;
        
        // Start marker
        ctx.beginPath();
        ctx.moveTo(startPixel, 0);
        ctx.lineTo(startPixel, height);
        ctx.stroke();
        
        // End marker
        ctx.beginPath();
        ctx.moveTo(endPixel, 0);
        ctx.lineTo(endPixel, height);
        ctx.stroke();
    }
    
    /**
     * Draw playback position indicator
     */
    drawPlaybackPosition() {
        if (!this.canvas || !this.canvasContext || !this.audioBuffer) {
            return;
        }
        
        const canvas = this.canvas;
        const ctx = this.canvasContext;
        const width = canvas.width;
        const height = canvas.height;
        const duration = this.audioBuffer.duration;
        
        // Calculate position in pixels on original timeline
        const positionPixel = (this.currentPlaybackPosition / duration) * width;
        
        // Draw playback position line
        ctx.strokeStyle = '#00ff00'; // Bright green
        ctx.lineWidth = 3;
        ctx.shadowColor = '#00ff00';
        ctx.shadowBlur = 5;
        
        ctx.beginPath();
        ctx.moveTo(positionPixel, 0);
        ctx.lineTo(positionPixel, height);
        ctx.stroke();
        
        // Reset shadow
        ctx.shadowBlur = 0;
    }
    
    /**
     * Update playback position during animation
     */
    updatePlaybackPosition() {
        if (!this.isPlaying) {
            return;
        }
        
        // Calculate current position in ORIGINAL timeline
        // The processed buffer duration is shorter/longer, but we map it back to original timeline
        const elapsedInProcessedBuffer = this.audioContext.currentTime - this.playbackStartTime;
        // Map elapsed time in processed buffer back to original timeline
        // If speed is 2x, processed buffer is half duration, so 1 second elapsed = 2 seconds in original
        const elapsedInOriginalTimeline = elapsedInProcessedBuffer * this.playbackSpeed;
        this.currentPlaybackPosition = this.startTime + elapsedInOriginalTimeline;
        
        // Re-render waveform with updated position
        if (this.canvas) {
            this.renderWaveform(this.canvas);
            this.drawPlaybackPosition();
        }
        
        // Continue animation
        this.playbackAnimationFrame = requestAnimationFrame(() => this.updatePlaybackPosition());
    }
    
    /**
     * Check if audio has been modified from initial state
     */
    hasAudioBeenModified() {
        if (!this.audioBuffer) return false;
        
        // Check if trim points changed
        if (Math.abs(this.startTime - this.initialStartTime) > 0.001) return true;
        if (Math.abs(this.endTime - this.initialEndTime) > 0.001) return true;
        
        // Check if speed changed
        if (Math.abs(this.playbackSpeed - this.initialPlaybackSpeed) > 0.001) return true;
        
        // Check if volume normalization enabled
        if (this.normalizeVolume) return true;
        
        return false;
    }
    
    /**
     * Set volume normalization parameters
     */
    setNormalization(enabled, targetLUFS = -14) {
        this.normalizeVolume = enabled;
        this.targetLUFS = targetLUFS;
        
        // Invalidate cached file size
        this.lastActualFileSize = null;
        
        // Recalculate gain if enabled
        if (enabled && this.audioBuffer) {
            this.calculateNormalizationGain();
        } else {
            this.gainMultiplier = 1.0;
        }
    }
    
    /**
     * Calculate RMS (Root Mean Square) loudness of audio buffer
     * This is a simplified loudness calculation
     */
    calculateRMS(audioBuffer) {
        const channels = audioBuffer.numberOfChannels;
        let sumSquares = 0;
        let totalSamples = 0;
        
        for (let ch = 0; ch < channels; ch++) {
            const channelData = audioBuffer.getChannelData(ch);
            for (let i = 0; i < channelData.length; i++) {
                sumSquares += channelData[i] * channelData[i];
                totalSamples++;
            }
        }
        
        const rms = Math.sqrt(sumSquares / totalSamples);
        return rms;
    }
    
    /**
     * Convert RMS to approximate LUFS
     * LUFS calculation is complex; this is a simplified approximation
     */
    rmsToLUFS(rms) {
        if (rms === 0) return -Infinity;
        // Approximate conversion: LUFS ≈ 20 * log10(RMS) - 0.691
        return 20 * Math.log10(rms) - 0.691;
    }
    
    /**
     * Calculate normalization gain needed to reach target LUFS
     */
    async calculateNormalizationGain() {
        if (!this.audioBuffer || !this.normalizeVolume) {
            this.gainMultiplier = 1.0;
            return 1.0;
        }
        
        // Extract trimmed portion for analysis
        const sampleRate = this.audioBuffer.sampleRate;
        const startSample = Math.floor(this.startTime * sampleRate);
        const endSample = Math.floor(this.endTime * sampleRate);
        const trimmedLength = endSample - startSample;
        const channels = this.audioBuffer.numberOfChannels;
        
        // Create a temporary buffer with trimmed audio
        const tempBuffer = this.audioContext.createBuffer(
            channels,
            trimmedLength,
            sampleRate
        );
        
        for (let ch = 0; ch < channels; ch++) {
            const sourceData = this.audioBuffer.getChannelData(ch);
            const destData = tempBuffer.getChannelData(ch);
            destData.set(sourceData.subarray(startSample, endSample));
        }
        
        // Calculate current loudness
        const currentRMS = this.calculateRMS(tempBuffer);
        const currentLUFS = this.rmsToLUFS(currentRMS);
        
        // Calculate gain needed
        // Target LUFS - Current LUFS = dB change needed
        // Gain = 10^(dB/20)
        const dbChange = this.targetLUFS - currentLUFS;
        this.gainMultiplier = Math.pow(10, dbChange / 20);
        
        // Limit gain to prevent clipping (max 6dB boost, unlimited attenuation)
        this.gainMultiplier = Math.min(this.gainMultiplier, 2.0);
        
        console.log(`Normalization: Current ${currentLUFS.toFixed(1)} LUFS, Target ${this.targetLUFS} LUFS, Gain ${this.gainMultiplier.toFixed(2)}x`);
        
        return this.gainMultiplier;
    }
    
    /**
     * Update trim region
     */
    setTrimRegion(startTime, endTime) {
        const duration = this.audioBuffer.duration;
        
        // Validate bounds
        this.startTime = Math.max(0, Math.min(startTime, duration));
        this.endTime = Math.max(this.startTime, Math.min(endTime, duration));
        
        // Ensure minimum duration
        if ((this.endTime - this.startTime) < this.minDuration) {
            this.endTime = Math.min(duration, this.startTime + this.minDuration);
        }
        
        // Invalidate cached file size
        this.lastActualFileSize = null;
        
        // Re-render waveform
        if (this.canvas) {
            this.renderWaveform(this.canvas);
        }
    }
    
    /**
     * Set playback speed
     */
    setPlaybackSpeed(speed) {
        this.playbackSpeed = Math.max(this.speedMin, Math.min(speed, this.speedMax));
        
        // Invalidate cached file size
        this.lastActualFileSize = null;
        
        // NOTE: Waveform doesn't need re-rendering as it always shows original audio
        // Speed only affects: 1) output duration stats, 2) playback position movement speed
        // The pitch-preserving time stretch is applied during export/playback via extractAndAdjustSpeed()
    }
    
    /**
     * Auto-fit to constraints (file size and duration)
     */
    autoFit() {
        if (!this.audioBuffer) return null;
        
        let duration = this.endTime - this.startTime;
        let speed = this.playbackSpeed;
        const channels = this.audioBuffer.numberOfChannels;
        
        // Calculate current file size
        let fileSize = this.calculateFileSize(duration, speed, channels);
        
        // Priority 1: Fit within file size limit
        if (fileSize > this.maxFileSize) {
            // Calculate minimum speed needed for 1MB
            const maxDuration = (this.maxFileSize - 44) / (this.targetSampleRate * 2 * channels);
            const requiredSpeed = duration / maxDuration;
            speed = Math.min(this.speedMax, Math.max(this.speedMin, requiredSpeed));
            
            // If even max speed can't fit in 1MB, must trim
            if (speed === this.speedMax) {
                fileSize = this.calculateFileSize(duration, this.speedMax, channels);
                if (fileSize > this.maxFileSize) {
                    // Calculate max trim duration that fits at max speed
                    const maxTrimDuration = ((this.maxFileSize - 44) / (this.targetSampleRate * 2 * channels)) * this.speedMax;
                    this.endTime = this.startTime + maxTrimDuration;
                    duration = maxTrimDuration;
                    
                    return {
                        trimmed: true,
                        speed: this.speedMax,
                        endTime: this.endTime,
                        message: `Audio trimmed to ${maxTrimDuration.toFixed(1)}s at ${this.speedMax}x speed to fit 1MB limit`
                    };
                }
            }
        }
        
        // Priority 2: Fit within time duration limit (if not already exceeded by size constraint)
        const effectiveDuration = duration / speed;
        if (effectiveDuration > this.maxDuration) {
            const requiredSpeed = duration / this.maxDuration;
            speed = Math.min(this.speedMax, Math.max(speed, requiredSpeed));
            
            // Re-check file size with new speed
            fileSize = this.calculateFileSize(duration, speed, channels);
            if (fileSize > this.maxFileSize) {
                // Need to trim to satisfy both constraints
                const maxDuration = Math.min(
                    this.maxDuration * this.speedMax,  // Max at max speed for time
                    ((this.maxFileSize - 44) / (this.targetSampleRate * 2 * channels)) * this.speedMax  // Max at max for size
                );
                this.endTime = this.startTime + maxDuration;
                duration = maxDuration;
                speed = this.speedMax;
                
                return {
                    trimmed: true,
                    speed: this.speedMax,
                    endTime: this.endTime,
                    message: `Audio optimized: trimmed to ${maxDuration.toFixed(1)}s at ${this.speedMax}x speed`
                };
            }
        }
        
        // Apply calculated speed
        this.setPlaybackSpeed(speed);
        
        return {
            trimmed: false,
            speed: speed,
            endTime: this.endTime,
            message: speed !== 1.0 ? `Optimized to ${speed.toFixed(2)}x speed` : 'Already within limits'
        };
    }
    
    /**
     * Calculate file size for given parameters
     * Optimized for mono WAV output
     */
    calculateFileSize(duration, speed, channels) {
        const effectiveDuration = duration / speed;
        const outputChannels = 1; // Always mono for smallest size
        // WAV size: sample_rate * bytes_per_sample * channels * duration + 44 byte header
        return Math.ceil(effectiveDuration * this.targetSampleRate * 2 * outputChannels + 44);
    }
    
    /**
     * Get current stats
     */
    getStats() {
        if (!this.audioBuffer) {
            return null;
        }
        
        const trimDuration = this.endTime - this.startTime;
        const effectiveDuration = trimDuration / this.playbackSpeed;
        const channels = this.audioBuffer.numberOfChannels;
        
        // Calculate estimated file size (may not be 100% accurate)
        const estimatedSize = this.calculateFileSize(trimDuration, this.playbackSpeed, channels);
        
        return {
            originalDuration: this.audioBuffer.duration,
            trimDuration: trimDuration,
            effectiveDuration: effectiveDuration,
            startTime: this.startTime,
            endTime: this.endTime,
            playbackSpeed: this.playbackSpeed,
            channels: channels,
            sampleRate: this.audioBuffer.sampleRate,
            estimatedFileSize: estimatedSize,
            withinSizeLimit: estimatedSize <= this.maxFileSize,
            withinDurationLimit: effectiveDuration <= this.maxDuration,
            // Store last actual size if available
            actualFileSize: this.lastActualFileSize || null
        };
    }
    
    /**
     * Get stats with actual file size by encoding the audio
     * This is more accurate but slower than getStats()
     */
    async getStatsWithActualSize() {
        if (!this.audioBuffer) {
            return null;
        }
        
        try {
            // Extract and encode to get actual size
            const trimmedBuffer = await this.extractAndAdjustSpeed();
            const wavBlob = this.encodeWAV(trimmedBuffer);
            const actualSize = wavBlob.size;
            
            // Cache the actual size
            this.lastActualFileSize = actualSize;
            
            const trimDuration = this.endTime - this.startTime;
            const effectiveDuration = trimDuration / this.playbackSpeed;
            const channels = this.audioBuffer.numberOfChannels;
            const estimatedSize = this.calculateFileSize(trimDuration, this.playbackSpeed, channels);
            
            return {
                originalDuration: this.audioBuffer.duration,
                trimDuration: trimDuration,
                effectiveDuration: effectiveDuration,
                startTime: this.startTime,
                endTime: this.endTime,
                playbackSpeed: this.playbackSpeed,
                channels: channels,
                sampleRate: this.audioBuffer.sampleRate,
                estimatedFileSize: estimatedSize,
                actualFileSize: actualSize,
                withinSizeLimit: actualSize <= this.maxFileSize,
                withinDurationLimit: effectiveDuration <= this.maxDuration
            };
        } catch (error) {
            console.error('Error calculating actual file size:', error);
            // Fall back to regular stats
            return this.getStats();
        }
    }
    
    /**
     * Play preview of trimmed audio with pitch-preserving speed and normalization applied
     */
    async playPreview() {
        if (!this.audioBuffer || this.isPlaying) {
            return;
        }
        
        try {
            // Generate processed audio (with speed change and normalization)
            const processedBuffer = await this.extractAndAdjustSpeed();
            
            console.log('Playback info:', {
                originalDuration: this.audioBuffer.duration,
                processedDuration: processedBuffer.duration,
                playbackSpeed: this.playbackSpeed,
                originalSampleRate: this.audioBuffer.sampleRate,
                processedSampleRate: processedBuffer.sampleRate
            });
            
            // Create buffer source
            this.sourceNode = this.audioContext.createBufferSource();
            this.sourceNode.buffer = processedBuffer;
            
            // CRITICAL: Ensure playbackRate is 1.0 to avoid pitch changes
            this.sourceNode.playbackRate.value = 1.0;
            
            this.sourceNode.connect(this.audioContext.destination);
            
            // Play entire processed buffer
            this.sourceNode.start(0);
            this.isPlaying = true;
            
            // Start playback position tracking
            this.playbackStartTime = this.audioContext.currentTime;
            this.currentPlaybackPosition = this.startTime;
            this.updatePlaybackPosition();
            
            // Auto-stop when done
            this.sourceNode.onended = () => {
                this.stopPreview();
            };
        } catch (error) {
            console.error('Error playing preview:', error);
            this.isPlaying = false;
        }
    }
    
    /**
     * Stop preview playback
     */
    stopPreview() {
        if (this.sourceNode && this.isPlaying) {
            try {
                this.sourceNode.stop();
            } catch (e) {
                // Already stopped
            }
        }
        
        this.isPlaying = false;
        this.sourceNode = null;
        
        // Cancel animation frame
        if (this.playbackAnimationFrame) {
            cancelAnimationFrame(this.playbackAnimationFrame);
            this.playbackAnimationFrame = null;
        }
        
        // Reset position and redraw
        this.currentPlaybackPosition = 0;
        if (this.canvas) {
            this.renderWaveform(this.canvas);
        }
    }
    
    /**
     * Export trimmed and speed-adjusted WAV
     */
    async exportWAV() {
        if (!this.audioBuffer) {
            throw new Error('No audio loaded');
        }
        
        const stats = this.getStats();
        
        // Validate constraints
        if (!stats.withinSizeLimit) {
            throw new Error(`File size ${(stats.estimatedFileSize / 1024 / 1024).toFixed(2)} MB exceeds 1MB limit`);
        }
        
        // Extract trimmed portion and apply speed adjustment
        const trimmedBuffer = await this.extractAndAdjustSpeed();
        
        // Encode as WAV
        const wavBlob = this.encodeWAV(trimmedBuffer);
        
        return wavBlob;
    }
    
    /**
     * Extract trimmed portion and apply speed adjustment
     */
    async extractAndAdjustSpeed() {
        const channels = this.audioBuffer.numberOfChannels;
        const sampleRate = this.audioBuffer.sampleRate;
        const startSample = Math.floor(this.startTime * sampleRate);
        const endSample = Math.floor(this.endTime * sampleRate);
        const trimmedLength = endSample - startSample;
        
        // If speed is 1.0, sample rate matches, and no normalization needed - just extract
        if (this.playbackSpeed === 1.0 && sampleRate === this.targetSampleRate && (!this.normalizeVolume || this.gainMultiplier === 1.0)) {
            const trimmedBuffer = this.audioContext.createBuffer(
                channels,
                trimmedLength,
                sampleRate
            );
            
            for (let ch = 0; ch < channels; ch++) {
                const sourceData = this.audioBuffer.getChannelData(ch);
                const destData = trimmedBuffer.getChannelData(ch);
                destData.set(sourceData.subarray(startSample, endSample));
            }
            
            return trimmedBuffer;
        }
        
        // Need to apply speed and/or resample - use pitch-preserving time stretch
        // First extract the trimmed portion
        const tempBuffer = this.audioContext.createBuffer(
            channels,
            trimmedLength,
            sampleRate
        );
        
        for (let ch = 0; ch < channels; ch++) {
            const sourceData = this.audioBuffer.getChannelData(ch);
            const destData = tempBuffer.getChannelData(ch);
            destData.set(sourceData.subarray(startSample, endSample));
        }
        
        // Apply pitch-preserving time stretch if speed != 1.0
        // Do NOT pass target sample rate - keep at original rate to preserve pitch
        let processedBuffer = tempBuffer;
        if (this.playbackSpeed !== 1.0) {
            processedBuffer = await this.pitchPreservingTimeStretch(tempBuffer, this.playbackSpeed);
        }
        
        // Always resample to target sample rate as a separate step (this doesn't affect pitch)
        const needsResampling = processedBuffer.sampleRate !== this.targetSampleRate;
        if (needsResampling) {
            const finalLength = Math.floor(processedBuffer.length * this.targetSampleRate / processedBuffer.sampleRate);
            const offlineContext = new OfflineAudioContext(
                channels,
                finalLength,
                this.targetSampleRate
            );
            
            const source = offlineContext.createBufferSource();
            source.buffer = processedBuffer;
            
            // Apply volume normalization if enabled
            if (this.normalizeVolume && this.gainMultiplier !== 1.0) {
                const gainNode = offlineContext.createGain();
                gainNode.gain.value = this.gainMultiplier;
                source.connect(gainNode);
                gainNode.connect(offlineContext.destination);
            } else {
                source.connect(offlineContext.destination);
            }
            
            source.start();
            
            const finalBuffer = await offlineContext.startRendering();
            return finalBuffer;
        } else {
            // No resampling needed, just apply normalization if enabled
            if (this.normalizeVolume && this.gainMultiplier !== 1.0) {
                return await this.applyGain(processedBuffer, this.gainMultiplier);
            }
            return processedBuffer;
        }
    }
    
    /**
     * Pitch-preserving time stretch using optimized WSOLA
     * Fast enough for real-time browser use with good quality
     */
    async pitchPreservingTimeStretch(audioBuffer, speed) {
        const channels = audioBuffer.numberOfChannels;
        const sampleRate = audioBuffer.sampleRate;
        const inputLength = audioBuffer.length;
        
        // For speed = 1.0, return unchanged
        if (Math.abs(speed - 1.0) < 0.001) {
            return audioBuffer;
        }
        
        const outputLength = Math.floor(inputLength / speed);
        
        console.log('Optimized WSOLA time stretch:', {
            inputSampleRate: sampleRate,
            inputLength: inputLength,
            speed: speed,
            outputLength: outputLength
        });
        
        // Create output buffer at same sample rate
        const outputBuffer = this.audioContext.createBuffer(
            channels,
            outputLength,
            sampleRate
        );
        
        // Optimized WSOLA parameters
        const grainSize = Math.floor(sampleRate * 0.02); // 20ms grains
        const overlapSize = Math.floor(grainSize * 0.5); // 50% overlap
        const seekWindow = Math.floor(sampleRate * 0.01); // 10ms search window
        const searchStep = Math.max(4, Math.floor(seekWindow / 10)); // Sparse search for speed
        
        // Pre-calculate Hann window
        const hannWindow = new Float32Array(overlapSize);
        for (let i = 0; i < overlapSize; i++) {
            hannWindow[i] = 0.5 * (1 - Math.cos(Math.PI * i / overlapSize));
        }
        
        for (let ch = 0; ch < channels; ch++) {
            const inputData = audioBuffer.getChannelData(ch);
            const outputData = outputBuffer.getChannelData(ch);
            
            let outputPos = 0;
            
            while (outputPos < outputLength) {
                const targetInputPos = Math.floor(outputPos * speed);
                
                if (targetInputPos + grainSize >= inputLength) break;
                
                let bestOffset = targetInputPos;
                
                // Cross-correlation search (optimized)
                if (outputPos > overlapSize) {
                    let bestCorrelation = -Infinity;
                    const searchStart = Math.max(0, targetInputPos - seekWindow);
                    const searchEnd = Math.min(inputLength - grainSize, targetInputPos + seekWindow);
                    
                    for (let offset = searchStart; offset <= searchEnd; offset += searchStep) {
                        let correlation = 0;
                        // Only correlate the overlap region
                        for (let i = 0; i < overlapSize; i++) {
                            correlation += outputData[outputPos - overlapSize + i] * inputData[offset + i];
                        }
                        
                        if (correlation > bestCorrelation) {
                            bestCorrelation = correlation;
                            bestOffset = offset;
                        }
                    }
                }
                
                // Copy grain with overlap-add
                const copyEnd = Math.min(grainSize, inputLength - bestOffset, outputLength - outputPos);
                
                for (let i = 0; i < copyEnd; i++) {
                    let sample = inputData[bestOffset + i];
                    
                    // Apply windowing only in overlap region
                    if (i < overlapSize && outputPos > 0) {
                        const existingSample = outputData[outputPos + i];
                        sample = sample * hannWindow[i] + existingSample * (1 - hannWindow[i]);
                    }
                    
                    outputData[outputPos + i] = sample;
                }
                
                outputPos += grainSize - overlapSize;
            }
        }
        
        console.log('Stretch complete');
        
        return outputBuffer;
    }
    
    /**
     * Apply gain to an audio buffer
     */
    async applyGain(audioBuffer, gain) {
        const channels = audioBuffer.numberOfChannels;
        const length = audioBuffer.length;
        const sampleRate = audioBuffer.sampleRate;
        
        const offlineContext = new OfflineAudioContext(channels, length, sampleRate);
        const source = offlineContext.createBufferSource();
        source.buffer = audioBuffer;
        
        const gainNode = offlineContext.createGain();
        gainNode.gain.value = gain;
        
        source.connect(gainNode);
        gainNode.connect(offlineContext.destination);
        source.start();
        
        return await offlineContext.startRendering();
    }
    
    /**
     * Encode AudioBuffer as WAV blob
     * Optimized for smallest file size: always outputs MONO
     */
    encodeWAV(audioBuffer) {
        const sourceChannels = audioBuffer.numberOfChannels;
        const sampleRate = audioBuffer.sampleRate;
        const length = audioBuffer.length;
        const bytesPerSample = 2; // 16-bit
        const outputChannels = 1; // Always mono for smallest file size
        
        // Get mono audio (mix down if stereo)
        let monoData;
        if (sourceChannels === 1) {
            monoData = audioBuffer.getChannelData(0);
        } else {
            // Mix stereo to mono
            const left = audioBuffer.getChannelData(0);
            const right = audioBuffer.getChannelData(1);
            monoData = new Float32Array(length);
            for (let i = 0; i < length; i++) {
                monoData[i] = (left[i] + right[i]) / 2;
            }
        }
        
        // Convert to 16-bit PCM
        const pcmData = new Int16Array(length);
        for (let i = 0; i < length; i++) {
            const s = Math.max(-1, Math.min(1, monoData[i]));
            pcmData[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }
        
        // Create WAV file
        const dataSize = pcmData.length * bytesPerSample;
        const buffer = new ArrayBuffer(44 + dataSize);
        const view = new DataView(buffer);
        
        // WAV header
        this.writeString(view, 0, 'RIFF');
        view.setUint32(4, 36 + dataSize, true);
        this.writeString(view, 8, 'WAVE');
        this.writeString(view, 12, 'fmt ');
        view.setUint32(16, 16, true); // PCM format chunk size
        view.setUint16(20, 1, true); // PCM format
        view.setUint16(22, outputChannels, true); // Mono
        view.setUint32(24, sampleRate, true);
        view.setUint32(28, sampleRate * outputChannels * bytesPerSample, true); // Byte rate
        view.setUint16(32, outputChannels * bytesPerSample, true); // Block align
        view.setUint16(34, 16, true); // Bits per sample
        this.writeString(view, 36, 'data');
        view.setUint32(40, dataSize, true);
        
        // Write PCM data
        const pcmView = new Int16Array(buffer, 44);
        pcmView.set(pcmData);
        
        return new Blob([buffer], { type: 'audio/wav' });
    }
    
    writeString(view, offset, string) {
        for (let i = 0; i < string.length; i++) {
            view.setUint8(offset + i, string.charCodeAt(i));
        }
    }
    
    /**
     * Export trimmed and speed-adjusted MP3
     * @param {number} bitrate - MP3 bitrate in kbps (e.g., 128, 192, 256, 320)
     */
    async exportMP3(bitrate = 128) {
        if (!this.audioBuffer) {
            throw new Error('No audio loaded');
        }
        
        // Check if lamejs is available
        if (typeof lamejs === 'undefined') {
            throw new Error('MP3 encoder not loaded');
        }
        
        // Extract trimmed portion and apply speed adjustment
        const trimmedBuffer = await this.extractAndAdjustSpeed();
        
        // Encode as MP3
        const mp3Blob = this.encodeMP3(trimmedBuffer, bitrate);
        
        return mp3Blob;
    }
    
    /**
     * Encode AudioBuffer as MP3 blob using lamejs
     */
    encodeMP3(audioBuffer, bitrate = 128) {
        const channels = audioBuffer.numberOfChannels;
        const sampleRate = audioBuffer.sampleRate;
        const length = audioBuffer.length;
        
        // Prepare samples for MP3 encoder
        const leftChannel = audioBuffer.getChannelData(0);
        const rightChannel = channels > 1 ? audioBuffer.getChannelData(1) : leftChannel;
        
        // Convert Float32Array to Int16Array
        const leftSamples = new Int16Array(length);
        const rightSamples = new Int16Array(length);
        
        for (let i = 0; i < length; i++) {
            leftSamples[i] = Math.max(-32768, Math.min(32767, leftChannel[i] * 32767));
            if (channels > 1) {
                rightSamples[i] = Math.max(-32768, Math.min(32767, rightChannel[i] * 32767));
            } else {
                rightSamples[i] = leftSamples[i];
            }
        }
        
        // Initialize MP3 encoder
        const mp3encoder = new lamejs.Mp3Encoder(channels, sampleRate, bitrate);
        const mp3Data = [];
        
        // Encode in chunks
        const sampleBlockSize = 1152; // Standard MP3 frame size
        for (let i = 0; i < length; i += sampleBlockSize) {
            const leftChunk = leftSamples.subarray(i, Math.min(i + sampleBlockSize, length));
            const rightChunk = rightSamples.subarray(i, Math.min(i + sampleBlockSize, length));
            
            // Only encode if we have samples
            if (leftChunk.length > 0) {
                const mp3buf = mp3encoder.encodeBuffer(leftChunk, rightChunk);
                if (mp3buf.length > 0) {
                    mp3Data.push(mp3buf);
                }
            }
        }
        
        // Finalize encoding
        const mp3buf = mp3encoder.flush();
        if (mp3buf.length > 0) {
            mp3Data.push(mp3buf);
        }
        
        // Create blob
        return new Blob(mp3Data, { type: 'audio/mp3' });
    }
    
    /**
     * Check if Web Audio API is supported
     */
    static isSupported() {
        return !!(window.AudioContext || window.webkitAudioContext);
    }
}

// Export for use in HTML
window.AudioTrimmer = AudioTrimmer;
