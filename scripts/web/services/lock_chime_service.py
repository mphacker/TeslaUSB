"""Service layer for lock chime management.

This module contains functions for validating, re-encoding, and managing
Tesla lock chime WAV files.
"""

import os
import subprocess
import time
import hashlib
import shutil
import wave
import contextlib
import logging

from config import MAX_LOCK_CHIME_SIZE

logger = logging.getLogger(__name__)


def validate_tesla_wav(file_path):
    """
    Validate WAV file meets Tesla's lock chime requirements:
    - Under 1MB in size
    - PCM encoding (uncompressed)
    - 16-bit
    - 44.1 kHz sample rate
    - Mono or stereo
    
    Returns: (is_valid, error_message)
    """
    try:
        # Check file size
        size_bytes = os.path.getsize(file_path)
        if size_bytes > MAX_LOCK_CHIME_SIZE:
            size_mb = size_bytes / (1024 * 1024)
            return False, f"File is {size_mb:.2f} MB. Tesla requires lock chimes to be under 1 MB."
        
        if size_bytes == 0:
            return False, "File is empty."
        
        # Check WAV format
        with contextlib.closing(wave.open(file_path, "rb")) as wav_file:
            params = wav_file.getparams()
            
            # Check sample width (16-bit = 2 bytes)
            if params.sampwidth != 2:
                bit_depth = params.sampwidth * 8
                return False, f"File is {bit_depth}-bit. Tesla requires 16-bit PCM."
            
            # Check sample rate (44.1 kHz or 48 kHz acceptable)
            if params.framerate not in (44100, 48000):
                rate_khz = params.framerate / 1000
                return False, f"Sample rate is {rate_khz:.1f} kHz. Tesla requires 44.1 or 48 kHz."
            
            # Check if it's PCM (compression type should be 'NONE')
            if params.comptype != 'NONE':
                return False, f"File uses {params.comptype} compression. Tesla requires uncompressed PCM."
            
            # Check channels (1 = mono, 2 = stereo - both acceptable)
            if params.nchannels not in (1, 2):
                return False, f"File has {params.nchannels} channels. Tesla requires mono or stereo."
        
        return True, "Valid"
        
    except (wave.Error, EOFError):
        return False, "Not a valid WAV file."
    except OSError as exc:
        return False, f"Unable to read file: {exc}"


def reencode_wav_for_tesla(input_path, output_path, progress_callback=None):
    """
    Re-encode a WAV file to meet Tesla's requirements using FFmpeg with multi-pass attempts:
    - Pass 1: 16-bit PCM, 44.1 kHz, mono (Tesla standard)
    - Pass 2: 16-bit PCM, 44.1 kHz, mono, trimmed to fit under 1MB
    
    Tesla Lock Chime Requirements:
    - PCM encoding
    - 16-bit
    - 44.1 kHz sample rate
    - Mono or stereo
    - 1MB maximum file size
    
    Returns: (success, message, details_dict)
    """
    # Define encoding strategies in order of preference
    strategies = [
        {
            "name": "Standard (16-bit, 44.1kHz, mono)",
            "args": ["-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1"],
            "trim": False
        },
        {
            "name": "Trimmed (16-bit, 44.1kHz, mono)",
            "args": ["-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1"],
            "trim": True
        }
    ]
    
    last_error = None
    
    for attempt, strategy in enumerate(strategies, 1):
        try:
            if progress_callback:
                progress_callback(f"Attempt {attempt}/{len(strategies)}: {strategy['name']}")
            
            # Build FFmpeg command
            cmd = ["ffmpeg", "-i", input_path]
            
            # If this strategy requires trimming, calculate the duration that fits in 1MB
            if strategy.get("trim"):
                # Calculate max duration: 1MB / (44100 Hz * 2 bytes * 1 channel + WAV header overhead)
                # PCM 16-bit = 2 bytes per sample, 44.1kHz = 44100 samples/sec, mono = 1 channel
                # 1MB = 1,048,576 bytes, subtract ~200 bytes for WAV headers
                max_bytes = MAX_LOCK_CHIME_SIZE - 200  # Leave room for WAV header
                bytes_per_second = 44100 * 2 * 1  # sample_rate * bytes_per_sample * channels
                max_duration = max_bytes / bytes_per_second
                
                if progress_callback:
                    progress_callback(f"Trimming audio to {max_duration:.1f} seconds to fit 1MB limit")
                
                # Add trim filter to keep only the first N seconds
                cmd.extend(["-t", str(max_duration)])
            
            cmd.extend(strategy["args"] + ["-y", output_path])
            
            # Use FFmpeg to re-encode
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=30,
                check=False
            )
            
            if result.returncode != 0:
                # Extract the actual error from FFmpeg output
                stderr_output = result.stderr.decode('utf-8', errors='ignore')
                
                # Check for read-only filesystem errors
                if 'read-only file system' in stderr_output.lower() or 'invalid argument' in stderr_output.lower():
                    return False, "Cannot write to filesystem (may be mounted read-only). Please ensure system is in Edit mode.", {}
                
                # FFmpeg errors typically appear after "Error" keyword or in last few lines
                error_lines = []
                for line in stderr_output.split('\n'):
                    line = line.strip()
                    if line and any(keyword in line.lower() for keyword in ['error', 'invalid', 'could not', 'failed', 'unable']):
                        error_lines.append(line)
                
                # If we found error lines, use the last few
                if error_lines:
                    error_msg = '. '.join(error_lines[-3:])[:300]
                else:
                    # Fall back to last non-empty lines
                    lines = [l.strip() for l in stderr_output.split('\n') if l.strip()]
                    error_msg = '. '.join(lines[-3:])[:300] if lines else "Unknown FFmpeg error"
                
                last_error = f"FFmpeg conversion failed: {error_msg}"
                continue  # Try next strategy
            
            # Check if output file was created and is not empty
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                last_error = "Re-encoding produced an empty file"
                continue
            
            # Check if re-encoded file is under size limit
            size_bytes = os.path.getsize(output_path)
            if size_bytes > MAX_LOCK_CHIME_SIZE:
                size_mb = size_bytes / (1024 * 1024)
                last_error = f"File still too large: {size_mb:.2f} MB (need < 1 MB)"
                
                # If not the last strategy, try next one (which will trim)
                if attempt < len(strategies):
                    continue
                else:
                    return False, f"Unable to fit file under 1 MB even after trimming. Final size: {size_mb:.2f} MB.", {}
            
            # Success! Return with details
            size_mb = size_bytes / (1024 * 1024)
            details = {
                "strategy": strategy["name"],
                "attempt": attempt,
                "size_mb": f"{size_mb:.2f}"
            }
            return True, f"Successfully re-encoded using {strategy['name']} (size: {size_mb:.2f} MB)", details
            
        except FileNotFoundError:
            return False, "FFmpeg is not installed on the system", {}
        except subprocess.TimeoutExpired:
            last_error = "Re-encoding timed out (file too large or complex)"
            continue
        except Exception as e:
            last_error = f"Re-encoding error: {str(e)}"
            continue
    
    # All strategies failed
    return False, f"All re-encoding attempts failed. Last error: {last_error}", {}


def replace_lock_chime(source_path, destination_path):
    """Swap in the selected WAV using temporary file to invalidate all caches."""
    src_size = os.path.getsize(source_path)

    if src_size == 0:
        raise ValueError("Selected WAV file is empty.")

    # Calculate MD5 hash of source file
    source_md5 = hashlib.md5()
    with open(source_path, "rb") as src_f:
        for chunk in iter(lambda: src_f.read(8192), b""):
            source_md5.update(chunk)
    source_hash = source_md5.hexdigest()

    dest_dir = os.path.dirname(destination_path)
    backup_path = os.path.join(dest_dir, "oldLockChime.wav")
    temp_path = os.path.join(dest_dir, ".LockChime.wav.tmp")

    # Drop any cached data BEFORE we start
    try:
        subprocess.run(
            ["sudo", "-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
            check=False,
            timeout=5
        )
    except Exception:
        pass

    # Backup existing file if present
    if os.path.isfile(destination_path):
        if os.path.isfile(backup_path):
            os.remove(backup_path)
        shutil.copyfile(destination_path, backup_path)
        
        # DELETE the old LockChime.wav completely
        os.remove(destination_path)
        
        # Sync the deletion multiple times to ensure it propagates
        subprocess.run(["sync"], check=False, timeout=5)
        time.sleep(0.5)
        subprocess.run(["sync"], check=False, timeout=5)
        time.sleep(0.5)

    try:
        # Write to a temporary file first with a different name
        # This ensures Windows never associates it with the old file
        shutil.copyfile(source_path, temp_path)
        temp_size = os.path.getsize(temp_path)
        if temp_size != src_size:
            raise IOError(
                f"Temp file size mismatch (expected {src_size} bytes, got {temp_size} bytes)."
            )
        
        # Sync the temp file completely
        with open(temp_path, "r+b") as temp_file:
            temp_file.flush()
            os.fsync(temp_file.fileno())
        
        subprocess.run(["sync"], check=False, timeout=10)
        time.sleep(0.5)
        
        # Now rename temp to final name - this creates a NEW directory entry
        # while the temp file data is already fully written
        os.rename(temp_path, destination_path)
        
        # Sync the directory metadata (the rename operation)
        try:
            dir_fd = os.open(dest_dir, os.O_RDONLY)
            os.fsync(dir_fd)
            os.close(dir_fd)
        except Exception:
            pass
        
        # Force sync of the destination file itself
        with open(destination_path, "r+b") as dest_file:
            dest_file.flush()
            os.fsync(dest_file.fileno())
        
        # Final full sync - critical for exFAT
        subprocess.run(["sync"], check=False, timeout=10)
        time.sleep(1.0)
        
        # Drop ALL caches again
        try:
            subprocess.run(
                ["sudo", "-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
                check=False,
                timeout=5
            )
        except Exception:
            pass
        
        # Update file access/modification times to force inode metadata change
        # This helps Tesla detect the file has changed even if size is the same
        try:
            current_time = time.time()
            os.utime(destination_path, (current_time, current_time))
            # Sync the metadata change
            subprocess.run(["sync"], check=False, timeout=5)
        except Exception:
            pass
        
        # Extra time for exFAT to settle and ensure all buffers are flushed
        time.sleep(0.5)
        
        # Verify the file contents match by comparing MD5 hashes
        dest_md5 = hashlib.md5()
        with open(destination_path, "rb") as dst_f:
            for chunk in iter(lambda: dst_f.read(8192), b""):
                dest_md5.update(chunk)
        dest_hash = dest_md5.hexdigest()
        
        if source_hash != dest_hash:
            raise IOError(
                f"File verification failed - MD5 mismatch after sync\n"
                f"Source: {source_hash}\n"
                f"Dest:   {dest_hash}"
            )
            
    except Exception:
        # Clean up temp file if it exists
        if os.path.isfile(temp_path):
            os.remove(temp_path)
        
        # Restore backup on failure
        if os.path.isfile(backup_path) and not os.path.isfile(destination_path):
            shutil.copyfile(backup_path, destination_path)
        raise

    # Clean up backup on success
    if os.path.isfile(backup_path):
        os.remove(backup_path)


def set_active_chime(chime_filename, part2_mount_path):
    """
    Set a chime from the Chimes/ library as the active lock chime.
    
    This is a mode-aware function that works in both Present and Edit modes:
    - In Edit mode: Uses normal file operations
    - In Present mode: Uses quick_edit_part2() to temporarily mount RW
    
    Args:
        chime_filename: Name of the chime file in Chimes/ folder
        part2_mount_path: Current mount path for part2 (RO or RW), can be None in present mode
    
    Returns:
        (success: bool, message: str)
    """
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from config import LOCK_CHIME_FILENAME, CHIMES_FOLDER
    
    mode = current_mode()
    logger.info(f"Setting active chime to {chime_filename} (mode: {mode})")
    
    # Validate chime exists (only in edit mode when we have access to mount)
    if mode == 'edit':
        if not part2_mount_path:
            return False, "Part2 mount path required in edit mode"
            
        chimes_dir = os.path.join(part2_mount_path, CHIMES_FOLDER)
        source_path = os.path.join(chimes_dir, chime_filename)
        
        if not os.path.isfile(source_path):
            return False, f"Chime file not found: {chime_filename}"
        
        # Validate it's a proper Tesla WAV
        is_valid, msg = validate_tesla_wav(source_path)
        if not is_valid:
            return False, f"Invalid chime file: {msg}"
    
    def _do_chime_replacement():
        """Internal function to perform the actual chime replacement."""
        try:
            # In quick edit mode, we need to use /mnt/gadget/part2 (RW mount)
            # Otherwise use the provided mount path
            if mode == 'present':
                from config import MNT_DIR
                rw_mount = os.path.join(MNT_DIR, 'part2')
                source = os.path.join(rw_mount, CHIMES_FOLDER, chime_filename)
                dest = os.path.join(rw_mount, LOCK_CHIME_FILENAME)
                
                # Validate file exists in present mode (we're inside quick_edit now)
                if not os.path.isfile(source):
                    return False, f"Chime file not found: {chime_filename}"
                
                # Validate it's a proper Tesla WAV
                is_valid, msg = validate_tesla_wav(source)
                if not is_valid:
                    return False, f"Invalid chime file: {msg}"
            else:
                source = os.path.join(part2_mount_path, CHIMES_FOLDER, chime_filename)
                dest = os.path.join(part2_mount_path, LOCK_CHIME_FILENAME)
            
            # Perform the replacement
            replace_lock_chime(source, dest)
            
            return True, f"Successfully set {chime_filename} as active lock chime"
            
        except Exception as e:
            logger.error(f"Error replacing chime: {e}", exc_info=True)
            return False, f"Error setting chime: {str(e)}"
    
    # Execute based on current mode
    if mode == 'present':
        # Use quick edit to temporarily mount RW
        logger.info("Using quick edit part2 for chime replacement")
        return quick_edit_part2(_do_chime_replacement)
    else:
        # Normal edit mode operation
        return _do_chime_replacement()
