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


def _file_md5(file_path):
    """Compute MD5 hash of a file."""
    digest = hashlib.md5()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def normalize_audio(input_path, target_lufs=-16):
    """
    Two-pass loudness normalization using FFmpeg's loudnorm filter.
    
    This ensures consistent playback volume across all chime files by normalizing
    to a target loudness level measured in LUFS (Loudness Units relative to Full Scale).
    
    Args:
        input_path: Path to the input WAV file
        target_lufs: Target loudness in LUFS (typically -23 to -12)
                    -23 = Broadcast standard (quiet)
                    -16 = Streaming services (recommended)
                    -14 = Apple Music (loud)
                    -12 = Maximum safe level
    
    Returns:
        Path to normalized temporary file
    
    Raises:
        Exception if normalization fails
    """
    import json
    import tempfile
    
    # First pass: measure loudness
    logger.info(f"Analyzing loudness for normalization (target: {target_lufs} LUFS)")
    
    result = subprocess.run([
        'ffmpeg', '-i', input_path,
        '-af', f'loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json',
        '-f', 'null', '-'
    ], capture_output=True, text=True, timeout=30)
    
    # Extract JSON from FFmpeg stderr output
    stderr = result.stderr
    json_start = stderr.rfind('{')
    if json_start == -1:
        raise ValueError("Could not find loudness analysis data in FFmpeg output")
    
    try:
        stats = json.loads(stderr[json_start:])
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse FFmpeg loudness stats: {e}")
        raise ValueError("Failed to analyze audio loudness")
    
    # Second pass: apply normalization with measured values
    temp_fd, temp_output = tempfile.mkstemp(suffix='.wav')
    os.close(temp_fd)  # Close the file descriptor, we just need the path
    
    try:
        logger.info(f"Applying normalization (measured input: {stats.get('input_i', 'N/A')} LUFS)")
        
        subprocess.run([
            'ffmpeg', '-i', input_path,
            '-af', (f'loudnorm=I={target_lufs}:TP=-1.5:LRA=11:'
                    f'measured_I={stats["input_i"]}:'
                    f'measured_LRA={stats["input_lra"]}:'
                    f'measured_TP={stats["input_tp"]}:'
                    f'measured_thresh={stats["input_thresh"]}:'
                    f'offset={stats["target_offset"]}'),
            '-ar', '44100',  # Tesla requirement
            '-y',  # Overwrite output
            temp_output
        ], check=True, capture_output=True, timeout=30)
        
        # Verify output was created
        if not os.path.exists(temp_output) or os.path.getsize(temp_output) == 0:
            raise ValueError("Normalization produced empty file")
        
        logger.info(f"Successfully normalized audio to {target_lufs} LUFS")
        return temp_output
        
    except subprocess.CalledProcessError as e:
        # Clean up temp file on error
        if os.path.exists(temp_output):
            os.remove(temp_output)
        stderr_msg = e.stderr.decode('utf-8', errors='ignore') if e.stderr else ''
        logger.error(f"FFmpeg normalization failed: {stderr_msg}")
        raise ValueError(f"Audio normalization failed: {stderr_msg[:200]}")
    except Exception as e:
        # Clean up temp file on error
        if os.path.exists(temp_output):
            os.remove(temp_output)
        raise


def replace_lock_chime(source_path, destination_path, source_md5=None):
    """Swap in the selected WAV using temporary file to invalidate all caches.
    
    Args:
        source_path: Path to the source WAV file
        destination_path: Path to the destination LockChime.wav
        source_md5: Optional pre-computed MD5 hash of source file (optimization)
    """
    src_size = os.path.getsize(source_path)

    if src_size == 0:
        raise ValueError("Selected WAV file is empty.")

    # Use pre-computed hash if provided, otherwise calculate it
    if source_md5 is None:
        source_md5_obj = hashlib.md5()
        with open(source_path, "rb") as src_f:
            for chunk in iter(lambda: src_f.read(65536), b""):
                source_md5_obj.update(chunk)
        source_hash = source_md5_obj.hexdigest()
    else:
        source_hash = source_md5

    dest_dir = os.path.dirname(destination_path)
    backup_path = os.path.join(dest_dir, "oldLockChime.wav")
    temp_path = os.path.join(dest_dir, ".LockChime.wav.tmp")
    
    # Clean up any orphaned temporary files from previous incomplete operations
    # This can happen if the process was killed or interrupted
    for orphan_file in [temp_path, backup_path]:
        if os.path.isfile(orphan_file):
            try:
                logger.warning(f"Removing orphaned temporary file: {os.path.basename(orphan_file)}")
                os.remove(orphan_file)
            except Exception as e:
                logger.error(f"Failed to remove orphaned file {orphan_file}: {e}")

    # Backup existing file if present
    if os.path.isfile(destination_path):
        if os.path.isfile(backup_path):
            os.remove(backup_path)
        shutil.copyfile(destination_path, backup_path)
        
        # DELETE the old LockChime.wav completely
        os.remove(destination_path)
        
        # Single sync after deletion with minimal delay
        subprocess.run(["sync"], check=False, timeout=5)
        time.sleep(0.1)

    try:
        # Write to a temporary file first with a different name
        # This ensures Windows never associates it with the old file
        shutil.copyfile(source_path, temp_path)
        temp_size = os.path.getsize(temp_path)
        if temp_size != src_size:
            raise IOError(
                f"Temp file size mismatch (expected {src_size} bytes, got {temp_size} bytes)."
            )
        
        # Sync the temp file data to disk
        with open(temp_path, "r+b") as temp_file:
            temp_file.flush()
            os.fsync(temp_file.fileno())
        
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
        
        # Update file access/modification times to force inode metadata change
        # This helps Tesla detect the file has changed even if size is the same
        try:
            current_time = time.time()
            os.utime(destination_path, (current_time, current_time))
        except Exception:
            pass
        
        # Final full sync - critical for exFAT durability
        subprocess.run(["sync"], check=False, timeout=10)
        
        # Minimal delay for filesystem to settle
        time.sleep(0.1)
        
        # Verify the file contents match by comparing MD5 hashes
        dest_md5 = hashlib.md5()
        with open(destination_path, "rb") as dst_f:
            for chunk in iter(lambda: dst_f.read(65536), b""):
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


def upload_chime_file(uploaded_file, filename, part2_mount_path=None, normalize=False, target_lufs=-16):
    """
    Upload a new chime file to the Chimes/ library.
    
    This is a mode-aware function that works in both Present and Edit modes:
    - In Edit mode: Uses normal file operations
    - In Present mode: Uses quick_edit_part2() to temporarily mount RW
    
    Strategy: In Present mode, do ALL processing (MP3 conversion, validation, re-encoding, normalization)
    in /tmp FIRST, then use quick_edit just to copy the final file. This keeps the
    quick_edit operation short (< 1 second) to avoid timeouts and process kills.
    
    Args:
        uploaded_file: FileStorage object from Flask request
        filename: Desired filename (must end in .wav)
        part2_mount_path: Current mount path for part2 (RO or RW), can be None in present mode
        normalize: Whether to apply volume normalization (default: False)
        target_lufs: Target loudness in LUFS if normalizing (default: -16)
    
    Returns:
        (success: bool, message: str)
    """
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from config import LOCK_CHIME_FILENAME, CHIMES_FOLDER
    import tempfile
    
    mode = current_mode()
    logger.info(f"Uploading chime file {filename} (mode: {mode})")
    
    # Validate filename
    if not filename.lower().endswith('.wav'):
        return False, "Filename must end with .wav"
    
    if filename.lower() == LOCK_CHIME_FILENAME.lower():
        return False, "Cannot upload a file named LockChime.wav. Please rename your file."
    
    # Determine file extension from uploaded file
    file_ext = os.path.splitext(uploaded_file.filename.lower())[1]
    if file_ext not in [".wav", ".mp3"]:
        return False, "Only WAV and MP3 files are allowed"
    
    # In Present mode, do all processing in /tmp first
    if mode == 'present':
        temp_dir = tempfile.mkdtemp(prefix='chime_upload_')
        try:
            # Save uploaded file to temp
            temp_input = os.path.join(temp_dir, 'input' + file_ext)
            uploaded_file.seek(0)
            uploaded_file.save(temp_input)
            
            # Convert MP3 to WAV if needed
            if file_ext == ".mp3":
                temp_wav = os.path.join(temp_dir, 'converted.wav')
                cmd = [
                    "ffmpeg", "-i", temp_input,
                    "-acodec", "pcm_s16le",  # 16-bit PCM
                    "-ar", "44100",          # 44.1 kHz
                    "-ac", "1",              # Mono
                    "-y",                    # Overwrite output
                    temp_wav
                ]
                
                result = subprocess.run(cmd, capture_output=True, timeout=60)
                if result.returncode != 0:
                    shutil.rmtree(temp_dir)
                    return False, "Failed to convert MP3 to WAV"
                
                os.remove(temp_input)
                temp_input = temp_wav
            
            # Validate and re-encode if needed
            is_valid, msg = validate_tesla_wav(temp_input)
            
            if not is_valid:
                logger.info(f"File needs re-encoding: {msg}")
                temp_output = os.path.join(temp_dir, 'reencoded.wav')
                success, reencode_msg, _ = reencode_wav_for_tesla(temp_input, temp_output)
                
                if not success:
                    shutil.rmtree(temp_dir)
                    return False, f"Upload failed: {reencode_msg}"
                
                temp_input = temp_output
                reencoded = True
            else:
                reencoded = False
            
            # Apply volume normalization if requested
            normalized = False
            if normalize:
                try:
                    logger.info(f"Applying volume normalization (target: {target_lufs} LUFS)")
                    temp_normalized = normalize_audio(temp_input, target_lufs)
                    
                    # Check if normalized file exceeds size limit
                    normalized_size = os.path.getsize(temp_normalized)
                    if normalized_size > MAX_LOCK_CHIME_SIZE:
                        logger.warning(f"Normalized file too large ({normalized_size} bytes), using original")
                        os.remove(temp_normalized)
                        # Continue with un-normalized file
                    else:
                        # Replace input with normalized version
                        os.remove(temp_input)
                        temp_input = temp_normalized
                        normalized = True
                except Exception as e:
                    logger.warning(f"Normalization failed: {e}, continuing without normalization")
                    # Continue with un-normalized file
            
            final_file = temp_input
            
            # Now do a quick copy operation
            def _do_quick_copy():
                """Quick file copy - should take < 1 second."""
                try:
                    from config import MNT_DIR
                    rw_mount = os.path.join(MNT_DIR, 'part2')
                    chimes_dir = os.path.join(rw_mount, CHIMES_FOLDER)
                    
                    # Create Chimes directory if needed
                    if not os.path.isdir(chimes_dir):
                        os.makedirs(chimes_dir, exist_ok=True)
                    
                    dest_path = os.path.join(chimes_dir, filename)
                    src_md5 = _file_md5(final_file)
                    src_size = os.path.getsize(final_file)

                    shutil.copy2(final_file, dest_path)

                    with open(dest_path, "r+b") as dst_f:
                        dst_f.flush()
                        os.fsync(dst_f.fileno())
                    # Sync directory entry
                    dir_fd = os.open(chimes_dir, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)

                    if not os.path.exists(dest_path):
                        return False, "Destination file missing after copy"

                    dest_size = os.path.getsize(dest_path)
                    if dest_size != src_size:
                        return False, f"Size mismatch after copy (expected {src_size}, got {dest_size})"

                    dest_md5 = _file_md5(dest_path)
                    if dest_md5 != src_md5:
                        return False, "MD5 mismatch after copy"

                    return True, "File copied and verified"
                except Exception as e:
                    logger.error(f"Error copying file: {e}", exc_info=True)
                    return False, f"Error copying file: {str(e)}"
            
            # Execute quick copy with short timeout
            logger.info("Using quick edit part2 for final file copy")
            success, copy_msg = quick_edit_part2(_do_quick_copy, timeout=30)
            
            # Clean up temp directory
            shutil.rmtree(temp_dir)
            
            if success:
                msg_parts = [f"Successfully uploaded {filename}"]
                if reencoded:
                    msg_parts.append("re-encoded for Tesla")
                if normalized:
                    # Map LUFS to friendly names
                    lufs_names = {-23: "Broadcast", -16: "Streaming", -14: "Loud", -12: "Maximum"}
                    level_name = lufs_names.get(target_lufs, f"{target_lufs} LUFS")
                    msg_parts.append(f"normalized to {level_name} level")
                
                if len(msg_parts) > 1:
                    return True, f"{msg_parts[0]} ({', '.join(msg_parts[1:])})"
                else:
                    return True, msg_parts[0]
            else:
                return False, copy_msg
                
        except subprocess.TimeoutExpired:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return False, "File conversion timed out"
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.error(f"Error uploading chime: {e}", exc_info=True)
            return False, f"Error uploading file: {str(e)}"
    
    else:
        # Edit mode - original logic (process directly on mounted filesystem)
        def _do_upload():
            """Internal function to perform the actual upload."""
            try:
                if not part2_mount_path:
                    return False, "Part2 mount path required in edit mode"
                
                rw_mount = part2_mount_path
                
                # Create Chimes directory if needed
                chimes_dir = os.path.join(rw_mount, CHIMES_FOLDER)
                if not os.path.isdir(chimes_dir):
                    os.makedirs(chimes_dir, exist_ok=True)
                
                dest_path = os.path.join(chimes_dir, filename)
                
                # Save to temporary location first
                temp_path = dest_path.replace('.wav', '_upload.wav')
                uploaded_file.seek(0)  # Reset file pointer
                uploaded_file.save(temp_path)
                
                # For MP3 files, convert to WAV first
                if file_ext == ".mp3":
                    mp3_temp_path = temp_path
                    temp_path = dest_path.replace('.wav', '_converted.wav')
                    
                    # Use FFmpeg to convert MP3 to WAV
                    cmd = [
                        "ffmpeg", "-i", mp3_temp_path,
                        "-acodec", "pcm_s16le",  # 16-bit PCM
                        "-ar", "44100",          # 44.1 kHz
                        "-ac", "1",              # Mono
                        "-y",                    # Overwrite output
                        temp_path
                    ]
                    
                    result = subprocess.run(cmd, capture_output=True, timeout=60)
                    os.remove(mp3_temp_path)  # Clean up MP3 temp file
                    
                    if result.returncode != 0:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                        return False, "Failed to convert MP3 to WAV"
                
                # Validate the file
                is_valid, msg = validate_tesla_wav(temp_path)
                
                if not is_valid:
                    # File needs re-encoding
                    logger.info(f"File needs re-encoding: {msg}")
                    temp_reencoded = dest_path.replace('.wav', '_reencoded.wav')
                    success, reencode_msg, _ = reencode_wav_for_tesla(temp_path, temp_reencoded)
                    
                    # Clean up original temp file
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    
                    if not success:
                        return False, f"Upload failed: {reencode_msg}"
                    
                    temp_path = temp_reencoded
                    reencoded = True
                else:
                    reencoded = False
                
                # Apply volume normalization if requested
                normalized = False
                if normalize:
                    try:
                        logger.info(f"Applying volume normalization (target: {target_lufs} LUFS)")
                        temp_normalized = normalize_audio(temp_path, target_lufs)
                        
                        # Check if normalized file exceeds size limit
                        normalized_size = os.path.getsize(temp_normalized)
                        if normalized_size > MAX_LOCK_CHIME_SIZE:
                            logger.warning(f"Normalized file too large ({normalized_size} bytes), using original")
                            os.remove(temp_normalized)
                            # Continue with un-normalized file
                        else:
                            # Replace temp with normalized version
                            os.remove(temp_path)
                            shutil.move(temp_normalized, temp_path)
                            normalized = True
                    except Exception as e:
                        logger.warning(f"Normalization failed: {e}, continuing without normalization")
                        # Continue with un-normalized file
                
                # Move to final location
                if os.path.exists(dest_path):
                    os.remove(dest_path)
                os.rename(temp_path, dest_path)

                # Verify integrity after move
                src_md5 = _file_md5(dest_path)
                dest_size = os.path.getsize(dest_path)

                with open(dest_path, "r+b") as dst_f:
                    dst_f.flush()
                    os.fsync(dst_f.fileno())
                dir_fd = os.open(chimes_dir, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
                
                # Build success message
                msg_parts = [f"Successfully uploaded {filename}"]
                if reencoded:
                    msg_parts.append("re-encoded for Tesla")
                if normalized:
                    # Map LUFS to friendly names
                    lufs_names = {-23: "Broadcast", -16: "Streaming", -14: "Loud", -12: "Maximum"}
                    level_name = lufs_names.get(target_lufs, f"{target_lufs} LUFS")
                    msg_parts.append(f"normalized to {level_name} level")
                
                if len(msg_parts) > 1:
                    return True, f"{msg_parts[0]} ({', '.join(msg_parts[1:])})"
                else:
                    return True, msg_parts[0]
                        
            except subprocess.TimeoutExpired:
                return False, "File conversion timed out"
            except Exception as e:
                logger.error(f"Error uploading chime: {e}", exc_info=True)
                return False, f"Error uploading file: {str(e)}"
        
        return _do_upload()


def save_pretrimmed_wav(uploaded_file, filename, part2_mount_path=None, normalize=False, target_lufs=-16):
    """
    Save a pre-trimmed WAV file that has already been processed by the browser-side audio trimmer.
    
    This function is optimized for files that are already:
    - PCM 16-bit WAV format
    - 44.1 kHz sample rate
    - Under 1MB file size
    - Properly trimmed and speed-adjusted
    
    Processing is minimal:
    - Optionally applies volume normalization (if requested)
    - Otherwise just validates and saves directly (no re-encoding needed)
    
    Args:
        uploaded_file: FileStorage object from Flask request (pre-trimmed WAV)
        filename: Desired filename (must end in .wav)
        part2_mount_path: Current mount path for part2 (RO or RW), can be None in present mode
        normalize: Whether to apply volume normalization (default: False)
        target_lufs: Target loudness in LUFS if normalizing (default: -16)
    
    Returns:
        (success: bool, message: str)
    """
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from config import LOCK_CHIME_FILENAME, CHIMES_FOLDER
    import tempfile
    
    mode = current_mode()
    logger.info(f"Saving pre-trimmed chime file {filename} (mode: {mode}, normalize: {normalize})")
    
    # Validate filename
    if not filename.lower().endswith('.wav'):
        return False, "Filename must end with .wav"
    
    if filename.lower() == LOCK_CHIME_FILENAME.lower():
        return False, "Cannot upload a file named LockChime.wav. Please rename your file."
    
    # In Present mode, process in /tmp first
    if mode == 'present':
        temp_dir = tempfile.mkdtemp(prefix='pretrimmed_chime_')
        try:
            # Save uploaded file to temp
            temp_input = os.path.join(temp_dir, 'pretrimmed.wav')
            uploaded_file.seek(0)
            uploaded_file.save(temp_input)
            
            # Quick validation
            is_valid, msg = validate_tesla_wav(temp_input)
            if not is_valid:
                shutil.rmtree(temp_dir)
                return False, f"Pre-trimmed file validation failed: {msg}"
            
            # Apply volume normalization if requested
            normalized = False
            if normalize:
                try:
                    logger.info(f"Applying volume normalization (target: {target_lufs} LUFS)")
                    temp_normalized = normalize_audio(temp_input, target_lufs)
                    
                    # Check if normalized file exceeds size limit
                    normalized_size = os.path.getsize(temp_normalized)
                    if normalized_size > MAX_LOCK_CHIME_SIZE:
                        logger.warning(f"Normalized file too large ({normalized_size} bytes), using original")
                        os.remove(temp_normalized)
                    else:
                        os.remove(temp_input)
                        temp_input = temp_normalized
                        normalized = True
                except Exception as e:
                    logger.warning(f"Normalization failed: {e}, continuing without normalization")
            
            final_file = temp_input
            
            # Quick copy operation
            def _do_quick_copy():
                """Quick file copy - should take < 1 second."""
                try:
                    from config import MNT_DIR
                    rw_mount = os.path.join(MNT_DIR, 'part2')
                    chimes_dir = os.path.join(rw_mount, CHIMES_FOLDER)
                    
                    # Create Chimes directory if needed
                    if not os.path.isdir(chimes_dir):
                        os.makedirs(chimes_dir, exist_ok=True)
                    
                    dest_path = os.path.join(chimes_dir, filename)
                    src_md5 = _file_md5(final_file)
                    src_size = os.path.getsize(final_file)

                    shutil.copy2(final_file, dest_path)

                    with open(dest_path, "r+b") as dst_f:
                        dst_f.flush()
                        os.fsync(dst_f.fileno())
                    
                    # Sync directory entry
                    dir_fd = os.open(chimes_dir, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)

                    if not os.path.exists(dest_path):
                        return False, "Destination file missing after copy"

                    dest_size = os.path.getsize(dest_path)
                    if dest_size != src_size:
                        return False, f"Size mismatch after copy (expected {src_size}, got {dest_size})"

                    dest_md5 = _file_md5(dest_path)
                    if dest_md5 != src_md5:
                        return False, "MD5 mismatch after copy"

                    return True, "File copied and verified"
                except Exception as e:
                    logger.error(f"Error copying file: {e}", exc_info=True)
                    return False, f"Error copying file: {str(e)}"
            
            # Execute quick copy
            logger.info("Using quick edit part2 for final file copy")
            success, copy_msg = quick_edit_part2(_do_quick_copy, timeout=30)
            
            # Clean up temp directory
            shutil.rmtree(temp_dir)
            
            if success:
                msg_parts = [f"Successfully uploaded {filename}"]
                if normalized:
                    lufs_names = {-23: "Broadcast", -16: "Streaming", -14: "Loud", -12: "Maximum"}
                    level_name = lufs_names.get(target_lufs, f"{target_lufs} LUFS")
                    msg_parts.append(f"normalized to {level_name} level")
                
                if len(msg_parts) > 1:
                    return True, f"{msg_parts[0]} ({', '.join(msg_parts[1:])})"
                else:
                    return True, msg_parts[0]
            else:
                return False, copy_msg
                
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.error(f"Error saving pre-trimmed chime: {e}", exc_info=True)
            return False, f"Error saving file: {str(e)}"
    
    else:
        # Edit mode - direct save
        try:
            if not part2_mount_path:
                return False, "Part2 mount path required in edit mode"
            
            rw_mount = part2_mount_path
            
            # Create Chimes directory if needed
            chimes_dir = os.path.join(rw_mount, CHIMES_FOLDER)
            if not os.path.isdir(chimes_dir):
                os.makedirs(chimes_dir, exist_ok=True)
            
            dest_path = os.path.join(chimes_dir, filename)
            
            # Save to temporary location first
            temp_path = dest_path.replace('.wav', '_upload.wav')
            uploaded_file.seek(0)
            uploaded_file.save(temp_path)
            
            # Quick validation
            is_valid, msg = validate_tesla_wav(temp_path)
            if not is_valid:
                os.remove(temp_path)
                return False, f"Pre-trimmed file validation failed: {msg}"
            
            # Apply volume normalization if requested
            normalized = False
            if normalize:
                try:
                    logger.info(f"Applying volume normalization (target: {target_lufs} LUFS)")
                    temp_normalized = normalize_audio(temp_path, target_lufs)
                    
                    # Check if normalized file exceeds size limit
                    normalized_size = os.path.getsize(temp_normalized)
                    if normalized_size > MAX_LOCK_CHIME_SIZE:
                        logger.warning(f"Normalized file too large ({normalized_size} bytes), using original")
                        os.remove(temp_normalized)
                    else:
                        os.remove(temp_path)
                        shutil.move(temp_normalized, temp_path)
                        normalized = True
                except Exception as e:
                    logger.warning(f"Normalization failed: {e}, continuing without normalization")
            
            # Move to final location
            if os.path.exists(dest_path):
                os.remove(dest_path)
            os.rename(temp_path, dest_path)

            # Sync
            with open(dest_path, "r+b") as dst_f:
                dst_f.flush()
                os.fsync(dst_f.fileno())
            dir_fd = os.open(chimes_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            
            # Build success message
            msg_parts = [f"Successfully uploaded {filename}"]
            if normalized:
                lufs_names = {-23: "Broadcast", -16: "Streaming", -14: "Loud", -12: "Maximum"}
                level_name = lufs_names.get(target_lufs, f"{target_lufs} LUFS")
                msg_parts.append(f"normalized to {level_name} level")
            
            if len(msg_parts) > 1:
                return True, f"{msg_parts[0]} ({', '.join(msg_parts[1:])})"
            else:
                return True, msg_parts[0]
                    
        except Exception as e:
            logger.error(f"Error saving pre-trimmed chime: {e}", exc_info=True)
            return False, f"Error saving file: {str(e)}"


def delete_chime_file(filename, part2_mount_path=None):
    """
    Delete a chime file from the Chimes/ library.
    Also deletes any schedules associated with this chime.
    
    This is a mode-aware function that works in both Present and Edit modes:
    - In Edit mode: Uses normal file operations
    - In Present mode: Uses quick_edit_part2() to temporarily mount RW
    
    Args:
        filename: Name of the chime file to delete
        part2_mount_path: Current mount path for part2 (RO or RW), can be None in present mode
    
    Returns:
        (success: bool, message: str)
    """
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from services.chime_scheduler_service import get_scheduler
    from config import CHIMES_FOLDER
    
    mode = current_mode()
    logger.info(f"Deleting chime file {filename} (mode: {mode})")
    
    # Sanitize filename
    filename = os.path.basename(filename)
    
    # First, delete any schedules associated with this chime
    try:
        scheduler = get_scheduler()
        schedules = scheduler.list_schedules()
        deleted_schedules = []
        
        for schedule in schedules:
            if schedule.get('chime_filename') == filename:
                scheduler.delete_schedule(schedule['id'])
                deleted_schedules.append(schedule['name'])
                logger.info(f"Deleted schedule '{schedule['name']}' associated with chime {filename}")
        
        if deleted_schedules:
            logger.info(f"Deleted {len(deleted_schedules)} schedule(s) for {filename}: {', '.join(deleted_schedules)}")
    except Exception as e:
        logger.warning(f"Error checking/deleting schedules for {filename}: {e}")
        # Continue with file deletion even if schedule deletion fails
    
    def _do_delete():
        """Internal function to perform the actual deletion."""
        try:
            # In quick edit mode, use /mnt/gadget/part2 (RW mount)
            # Otherwise use the provided mount path
            if mode == 'present':
                from config import MNT_DIR
                rw_mount = os.path.join(MNT_DIR, 'part2')
            else:
                if not part2_mount_path:
                    return False, "Part2 mount path required in edit mode"
                rw_mount = part2_mount_path
            
            chimes_dir = os.path.join(rw_mount, CHIMES_FOLDER)
            file_path = os.path.join(chimes_dir, filename)
            
            if not os.path.isfile(file_path):
                return False, "File not found"
            
            os.remove(file_path)
            return True, f"Successfully deleted {filename}"
            
        except Exception as e:
            logger.error(f"Error deleting chime: {e}", exc_info=True)
            return False, f"Error deleting file: {str(e)}"
    
    # Execute based on current mode
    if mode == 'present':
        # Use quick edit to temporarily mount RW
        logger.info("Using quick edit part2 for chime deletion")
        return quick_edit_part2(_do_delete, timeout=30)
    else:
        # Normal edit mode operation
        return _do_delete()


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
            
            # Pre-compute MD5 to optimize replace_lock_chime
            source_hash = _file_md5(source)
            
            # Perform the replacement
            replace_lock_chime(source, dest, source_md5=source_hash)
            
            return True, f"Successfully set {chime_filename} as active lock chime"
            
        except Exception as e:
            logger.error(f"Error replacing chime: {e}", exc_info=True)
            return False, f"Error setting chime: {str(e)}"
    
    # Execute based on current mode
    if mode == 'present':
        # Use quick edit to temporarily mount RW
        logger.info("Using quick edit part2 for chime replacement")
        return quick_edit_part2(_do_chime_replacement, timeout=30)
    else:
        # Normal edit mode operation
        return _do_chime_replacement()


def rename_chime_file(old_filename, new_filename):
    """
    Rename a lock chime file without re-encoding.
    
    This is used when the user only changes the filename in the trim editor
    without modifying the audio content. Uses mode-aware operations.
    
    Args:
        old_filename: Current filename (e.g., "chime.wav")
        new_filename: New filename (e.g., "my_chime.wav")
        
    Returns:
        dict: {"success": True/False, "message": str}
    """
    from services.partition_service import get_mount_path
    from services.mode_service import current_mode
    from services.partition_mount_service import quick_edit_part2
    from config import CHIMES_FOLDER
    
    # Validate filenames
    if not old_filename or not new_filename:
        return {"success": False, "error": "Both old and new filenames are required"}
    
    # Ensure .wav extension
    if not new_filename.lower().endswith('.wav'):
        new_filename += '.wav'
    
    # Check for invalid characters
    import re
    if re.search(r'[<>:"|?*\\/]', new_filename):
        return {"success": False, "error": "Filename contains invalid characters"}
    
    mode = current_mode()
    part2_mount_path = get_mount_path("part2")
    
    if not part2_mount_path:
        return {"success": False, "error": "Part2 not mounted"}
    
    def _do_rename():
        """Internal function to perform the actual rename."""
        try:
            # In quick edit mode (present), use RW mount path
            if mode == 'present':
                from config import MNT_DIR
                rw_mount = os.path.join(MNT_DIR, 'part2')
                chimes_dir = os.path.join(rw_mount, CHIMES_FOLDER)
            else:
                chimes_dir = os.path.join(part2_mount_path, CHIMES_FOLDER)
            
            old_path = os.path.join(chimes_dir, old_filename)
            new_path = os.path.join(chimes_dir, new_filename)
            
            # Validate old file exists
            if not os.path.isfile(old_path):
                return {"success": False, "error": f"File not found: {old_filename}"}
            
            # Validate new filename doesn't already exist
            if os.path.exists(new_path):
                return {"success": False, "error": f"File already exists: {new_filename}"}
            
            # Perform the rename
            os.rename(old_path, new_path)
            
            # Sync to ensure write completes
            os.sync()
            
            logger.info(f"Renamed chime: {old_filename} -> {new_filename}")
            return {"success": True, "message": f"Renamed to {new_filename}"}
            
        except Exception as e:
            logger.error(f"Error renaming chime: {e}", exc_info=True)
            return {"success": False, "error": f"Error renaming file: {str(e)}"}
    
    # Execute based on current mode
    if mode == 'present':
        logger.info("Using quick edit part2 for chime rename")
        return quick_edit_part2(_do_rename, timeout=20)
    else:
        return _do_rename()
