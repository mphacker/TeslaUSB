#!/usr/bin/env python3
"""
Analytics service for TeslaUSB storage and video statistics.

Provides functions for:
- Disk usage calculation and visualization
- Video statistics by folder (count, total size, oldest/newest)
- Storage health monitoring and alerts
- Recording time estimation
"""

import os
import shutil
from datetime import datetime
from collections import defaultdict

from config import MNT_DIR, RO_MNT_DIR, VIDEO_EXTENSIONS
from services.mode_service import current_mode
from services.partition_service import iter_all_partitions


def get_partition_usage():
    """
    Get disk usage statistics for all partitions.
    
    Returns:
        dict: Partition usage information with keys:
            - part1/part2: {
                'total_gb': float,
                'used_gb': float,
                'free_gb': float,
                'percent_used': float,
                'path': str
              }
    """
    usage = {}
    
    for partition_name, mount_path in iter_all_partitions():
        try:
            stat = shutil.disk_usage(mount_path)
            usage[partition_name] = {
                'total_gb': stat.total / (1024**3),
                'used_gb': stat.used / (1024**3),
                'free_gb': stat.free / (1024**3),
                'percent_used': (stat.used / stat.total * 100) if stat.total > 0 else 0,
                'path': mount_path
            }
        except Exception as e:
            # Partition not accessible
            usage[partition_name] = {
                'total_gb': 0,
                'used_gb': 0,
                'free_gb': 0,
                'percent_used': 0,
                'path': mount_path,
                'error': str(e)
            }
    
    return usage


def get_video_statistics():
    """
    Get detailed video statistics for TeslaCam folders.
    
    Returns:
        dict: Video statistics with keys:
            - folders: {folder_name: {count, total_size_gb, oldest, newest}}
            - totals: {total_videos, total_size_gb}
    """
    mode = current_mode()
    stats = {
        'folders': {},
        'totals': {
            'total_videos': 0,
            'total_size_gb': 0
        }
    }
    
    # Get TeslaCam path based on mode
    if mode == "present":
        teslacam_path = os.path.join(RO_MNT_DIR, "part1-ro", "TeslaCam")
    elif mode == "edit":
        teslacam_path = os.path.join(MNT_DIR, "part1", "TeslaCam")
    else:
        return stats
    
    if not os.path.isdir(teslacam_path):
        return stats
    
    # Scan each folder
    try:
        for folder_name in os.listdir(teslacam_path):
            folder_path = os.path.join(teslacam_path, folder_name)
            if not os.path.isdir(folder_path):
                continue
            
            folder_stats = {
                'count': 0,
                'total_size_bytes': 0,
                'oldest': None,
                'newest': None,
                'oldest_timestamp': None,
                'newest_timestamp': None
            }
            
            # Scan videos in folder
            for filename in os.listdir(folder_path):
                if not any(filename.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
                    continue
                
                filepath = os.path.join(folder_path, filename)
                try:
                    stat = os.stat(filepath)
                    folder_stats['count'] += 1
                    folder_stats['total_size_bytes'] += stat.st_size
                    
                    mtime = stat.st_mtime
                    if folder_stats['oldest_timestamp'] is None or mtime < folder_stats['oldest_timestamp']:
                        folder_stats['oldest_timestamp'] = mtime
                        folder_stats['oldest'] = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
                    
                    if folder_stats['newest_timestamp'] is None or mtime > folder_stats['newest_timestamp']:
                        folder_stats['newest_timestamp'] = mtime
                        folder_stats['newest'] = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
                        
                except Exception:
                    continue
            
            # Convert to GB
            folder_stats['total_size_gb'] = folder_stats['total_size_bytes'] / (1024**3)
            
            stats['folders'][folder_name] = folder_stats
            stats['totals']['total_videos'] += folder_stats['count']
            stats['totals']['total_size_gb'] += folder_stats['total_size_gb']
    
    except Exception:
        pass
    
    return stats


def get_storage_health():
    """
    Analyze storage health and generate alerts.
    
    Returns:
        dict: Health information with keys:
            - status: 'healthy', 'warning', 'critical'
            - alerts: list of alert messages
            - recommendations: list of recommended actions
    """
    usage = get_partition_usage()
    health = {
        'status': 'healthy',
        'alerts': [],
        'recommendations': []
    }
    
    # Check each partition
    for partition_name, stats in usage.items():
        if 'error' in stats:
            health['status'] = 'critical'
            health['alerts'].append(f"{partition_name}: Not accessible - {stats['error']}")
            continue
        
        percent = stats['percent_used']
        
        if percent >= 95:
            health['status'] = 'critical'
            health['alerts'].append(f"{partition_name}: Critical storage ({percent:.1f}% used)")
            health['recommendations'].append(f"Delete videos from {partition_name} immediately")
        elif percent >= 90:
            if health['status'] != 'critical':
                health['status'] = 'warning'
            health['alerts'].append(f"{partition_name}: Low storage ({percent:.1f}% used)")
            health['recommendations'].append(f"Consider cleaning up old videos from {partition_name}")
        elif percent >= 80:
            if health['status'] == 'healthy':
                health['status'] = 'warning'
            health['alerts'].append(f"{partition_name}: Storage usage at {percent:.1f}%")
    
    return health


def estimate_recording_time():
    """
    Estimate remaining recording time based on recent usage patterns.
    
    Returns:
        dict: Recording time estimates with keys:
            - teslacam_hours: estimated hours of recording remaining
            - method: how the estimate was calculated
            - confidence: 'high', 'medium', 'low'
    """
    usage = get_partition_usage()
    video_stats = get_video_statistics()
    
    estimate = {
        'teslacam_hours': None,
        'method': 'unknown',
        'confidence': 'low'
    }
    
    # Get part1 (TeslaCam) free space
    if 'part1' not in usage or 'error' in usage['part1']:
        return estimate
    
    free_gb = usage['part1']['free_gb']
    
    # Calculate average video size
    total_videos = video_stats['totals']['total_videos']
    total_size_gb = video_stats['totals']['total_size_gb']
    
    if total_videos == 0:
        # No videos yet, use rough estimate
        # Tesla records ~400 MB/hour (4 cameras @ 1080p)
        estimate['teslacam_hours'] = free_gb / 0.4
        estimate['method'] = 'theoretical (400 MB/hour)'
        estimate['confidence'] = 'low'
    else:
        # Use actual average
        avg_size_gb = total_size_gb / total_videos
        
        # Tesla videos are typically 1-minute clips
        # Calculate how many 1-minute clips fit in free space
        clips_remaining = free_gb / avg_size_gb if avg_size_gb > 0 else 0
        minutes_remaining = clips_remaining * 1  # 1 minute per clip
        hours_remaining = minutes_remaining / 60
        
        estimate['teslacam_hours'] = hours_remaining
        estimate['method'] = f'based on {total_videos} existing videos'
        estimate['confidence'] = 'high' if total_videos > 100 else 'medium'
    
    return estimate


def get_folder_breakdown():
    """
    Get detailed breakdown of storage usage by folder type.
    
    Returns:
        list: List of folder statistics, sorted by size descending
    """
    video_stats = get_video_statistics()
    breakdown = []
    
    # Define folder types and descriptions
    folder_info = {
        'RecentClips': {
            'description': 'Recent driving footage',
            'icon': '🚗',
            'priority': 'low'
        },
        'SavedClips': {
            'description': 'Manually saved clips',
            'icon': '⭐',
            'priority': 'high'
        },
        'SentryClips': {
            'description': 'Sentry mode recordings',
            'icon': '🛡️',
            'priority': 'high'
        }
    }
    
    for folder_name, stats in video_stats['folders'].items():
        info = folder_info.get(folder_name, {
            'description': 'Unknown folder type',
            'icon': '📁',
            'priority': 'medium'
        })
        
        breakdown.append({
            'name': folder_name,
            'count': stats['count'],
            'size_gb': stats['total_size_gb'],
            'size_percent': (stats['total_size_gb'] / video_stats['totals']['total_size_gb'] * 100) 
                           if video_stats['totals']['total_size_gb'] > 0 else 0,
            'oldest': stats['oldest'],
            'newest': stats['newest'],
            'description': info['description'],
            'icon': info['icon'],
            'priority': info['priority']
        })
    
    # Sort by size descending
    breakdown.sort(key=lambda x: x['size_gb'], reverse=True)
    
    return breakdown


def get_complete_analytics():
    """
    Get all analytics data in one call.
    
    Returns:
        dict: Complete analytics dashboard data
    """
    return {
        'partition_usage': get_partition_usage(),
        'video_statistics': get_video_statistics(),
        'storage_health': get_storage_health(),
        'recording_estimate': estimate_recording_time(),
        'folder_breakdown': get_folder_breakdown(),
        'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
