#!/usr/bin/python3
"""Prune only completed recordings created by this NVR."""
from pathlib import Path
import os
import shutil
import stat
import time

def prune(root,settings):
    root=Path(root)
    max_bytes=float(settings['storage_gb'])*1024**3
    min_free=float(settings['reserve_gb'])*1024**3
    now=time.time()
    files=[]
    total=0
    for directory, folders, names in os.walk(root,followlinks=False):
        folders[:]=[n for n in folders if n!='.telegram-spool' and not (Path(directory)/n).is_symlink()]
        for name in names:
            p=Path(directory)/name
            if p.suffix.lower() not in {'.mp4','.mkv','.avi','.mov','.jpg','.jpeg','.thumb'}: continue
            try: s=p.lstat()
            except FileNotFoundError: continue
            if not stat.S_ISREG(s.st_mode): continue
            total+=s.st_size
            if s.st_mtime < now-600: files.append((s.st_mtime,p,s.st_size))
    free=shutil.disk_usage(root).free
    count=0
    for mtime,p,size in sorted(files):
        if mtime>=now-settings['retention_days']*86400 and total<=max_bytes and free>=min_free: continue
        try: p.unlink()
        except FileNotFoundError: continue
        count+=1
        total-=size
        free+=size
    if count: print(f'Removed {count} completed media files; remaining media bytes: {total}')
    
