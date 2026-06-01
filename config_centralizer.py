#!/usr/bin/env python3
"""
Config Centralizer - Scan, Import, and Manage All Configs
Finds all your scattered configs and centralizes them in one place
"""

import os
import json
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional
import hashlib
import re

class ConfigScanner:
    """Scans system for configuration files"""
    
    def __init__(self):
        self.home = Path.home()
        self.results = []
        
        # Common config patterns
        self.config_patterns = [
            '*.conf',
            '*.config',
            '*.cfg',
            '*.json',
            '*.yaml',
            '*.yml',
            '*.toml',
            '*.ini',
            '*.env',
            '*rc',  # bashrc, vimrc, etc.
            '*.service',
            '*.timer',
        ]
        
        # API key patterns
        self.api_key_patterns = [
            '*api*key*',
            '*token*',
            '*secret*',
            '*credentials*',
            '*password*',
        ]
        
        # Locations to scan
        self.scan_locations = [
            self.home / '.config',
            self.home / '.local/share',
            self.home / '.ssh',
            self.home / '.aws',
            self.home / '.kube',
            self.home / 'config',  # If you have a top-level config dir
        ]
        
        # Systemd user services
        systemd_user = self.home / '.config/systemd/user'
        if systemd_user.exists():
            self.scan_locations.append(systemd_user)
        
        # Skip these (too noisy or temp)
        self.skip_patterns = [
            '*cache*',
            '*Cache*',
            '*.log',
            '*logs*',
            '*tmp*',
            '*temp*',
            '*.lock',
            '*.pid',
            '*node_modules*',
            '*__pycache__*',
            '*.pyc',
            '*venv*',
            '*.git*',
        ]
    
    def should_skip(self, path: Path) -> bool:
        """Check if path should be skipped"""
        path_str = str(path)
        
        # Skip patterns
        for pattern in self.skip_patterns:
            if Path(path_str).match(pattern):
                return True
        
        # Skip if too large (>10MB - probably not a config)
        try:
            if path.is_file() and path.stat().st_size > 10 * 1024 * 1024:
                return True
        except:
            return True
        
        return False
    
    def categorize_config(self, path: Path) -> Dict:
        """Categorize and score a config file"""
        path_str = str(path).lower()
        name = path.name.lower()
        
        category = 'unknown'
        importance = 1  # 1-5 scale
        sensitive = False
        
        # API keys / secrets
        if any(pattern in path_str for pattern in ['api', 'key', 'token', 'secret', 'password', 'credential']):
            category = 'secret'
            importance = 5
            sensitive = True
        
        # Systemd services
        elif path.suffix in ['.service', '.timer', '.socket']:
            category = 'service'
            importance = 4
        
        # Application configs
        elif '.config' in path_str or 'config' in name:
            category = 'application'
            importance = 3
        
        # SSH configs
        elif '.ssh' in path_str:
            category = 'ssh'
            importance = 5
            sensitive = True
        
        # Shell configs
        elif name.endswith('rc') or name in ['profile', 'bash_profile', 'zshrc', 'bashrc']:
            category = 'shell'
            importance = 4
        
        # Environment files
        elif '.env' in name:
            category = 'environment'
            importance = 4
            sensitive = True
        
        # General configs
        elif path.suffix in ['.conf', '.cfg', '.ini', '.yaml', '.yml', '.toml', '.json']:
            category = 'config'
            importance = 3
        
        return {
            'path': path,
            'category': category,
            'importance': importance,
            'sensitive': sensitive,
            'size': path.stat().st_size if path.is_file() else 0,
            'modified': datetime.fromtimestamp(path.stat().st_mtime) if path.exists() else None,
            'recommend_import': importance >= 3,
        }
    
    def scan(self, progress_callback=None) -> List[Dict]:
        """Scan system for configs"""
        print("🔍 Scanning for configuration files...")
        self.results = []
        total_found = 0
        
        for location in self.scan_locations:
            if not location.exists():
                continue
            
            if progress_callback:
                progress_callback(f"Scanning {location}...")
            
            print(f"  Scanning: {location}")
            
            # Walk directory
            for root, dirs, files in os.walk(location):
                root_path = Path(root)
                
                # Skip hidden dirs unless they're important
                dirs[:] = [d for d in dirs if not d.startswith('.') or d in ['.ssh', '.aws', '.kube', '.config']]
                
                for file in files:
                    file_path = root_path / file
                    
                    if self.should_skip(file_path):
                        continue
                    
                    # Check if matches any config pattern
                    matches = False
                    for pattern in self.config_patterns + self.api_key_patterns:
                        if file_path.match(pattern):
                            matches = True
                            break
                    
                    if matches:
                        try:
                            info = self.categorize_config(file_path)
                            self.results.append(info)
                            total_found += 1
                            
                            if progress_callback and total_found % 10 == 0:
                                progress_callback(f"Found {total_found} configs...")
                        except Exception as e:
                            print(f"    Warning: Could not process {file_path}: {e}")
        
        # Sort by importance
        self.results.sort(key=lambda x: (-x['importance'], str(x['path'])))
        
        print(f"✓ Found {len(self.results)} configuration files")
        return self.results
    
    def get_summary(self) -> Dict:
        """Get summary statistics"""
        if not self.results:
            return {}
        
        categories = {}
        total_size = 0
        recommended = 0
        sensitive = 0
        
        for item in self.results:
            cat = item['category']
            categories[cat] = categories.get(cat, 0) + 1
            total_size += item['size']
            if item['recommend_import']:
                recommended += 1
            if item['sensitive']:
                sensitive += 1
        
        return {
            'total': len(self.results),
            'categories': categories,
            'total_size_mb': total_size / (1024 * 1024),
            'recommended': recommended,
            'sensitive': sensitive,
        }


class ConfigCentralizer:
    """Manages centralized config storage"""
    
    def __init__(self, master_dir: Optional[Path] = None):
        self.master_dir = master_dir or (Path.home() / '.config/lifeOS/master')
        self.deployed_dir = self.master_dir.parent / 'deployed'
        self.mapping_file = self.master_dir.parent / 'config-mapping.json'
        
        # Create master structure
        self.master_dir.mkdir(parents=True, exist_ok=True)
        self.deployed_dir.mkdir(parents=True, exist_ok=True)
        
        # Subdirectories
        (self.master_dir / 'apis').mkdir(exist_ok=True)
        (self.master_dir / 'services').mkdir(exist_ok=True)
        (self.master_dir / 'applications').mkdir(exist_ok=True)
        (self.master_dir / 'system').mkdir(exist_ok=True)
        (self.master_dir / 'secrets').mkdir(exist_ok=True)
        (self.master_dir / 'shell').mkdir(exist_ok=True)
        
        # Load or create mapping
        self.mapping = self.load_mapping()
    
    def load_mapping(self) -> Dict:
        """Load config mapping"""
        if self.mapping_file.exists():
            with open(self.mapping_file) as f:
                return json.load(f)
        return {'configs': {}}
    
    def save_mapping(self):
        """Save config mapping"""
        with open(self.mapping_file, 'w') as f:
            json.dump(self.mapping, f, indent=2)
    
    def get_master_path(self, original_path: Path, category: str) -> Path:
        """Determine master path for a config"""
        name = original_path.name
        
        # Map category to subdirectory
        category_map = {
            'secret': 'secrets',
            'service': 'services',
            'application': 'applications',
            'ssh': 'secrets',
            'shell': 'shell',
            'environment': 'applications',
            'config': 'applications',
            'unknown': 'applications',
        }
        
        subdir = category_map.get(category, 'applications')
        
        # Handle name conflicts
        base_path = self.master_dir / subdir / name
        if base_path.exists():
            # Add parent dir name to differentiate
            parent_name = original_path.parent.name
            name = f"{parent_name}_{name}"
            base_path = self.master_dir / subdir / name
        
        return base_path
    
    def import_config(self, config_info: Dict, backup: bool = True) -> tuple[bool, str]:
        """Import a config file to master"""
        original_path = config_info['path']
        category = config_info['category']
        
        try:
            # Determine master location
            master_path = self.get_master_path(original_path, category)
            
            # Backup original if requested
            if backup:
                backup_path = Path(str(original_path) + '.backup')
                if not backup_path.exists():
                    shutil.copy2(original_path, backup_path)
            
            # Copy to master
            shutil.copy2(original_path, master_path)
            
            # Record mapping
            self.mapping['configs'][str(master_path)] = {
                'original': str(original_path),
                'category': category,
                'imported': datetime.now().isoformat(),
                'sensitive': config_info['sensitive'],
            }
            self.save_mapping()
            
            return True, f"Imported to {master_path}"
        
        except Exception as e:
            return False, f"Error: {e}"
    
    def create_symlink(self, master_path: Path, original_path: Path) -> tuple[bool, str]:
        """Create symlink from original location to master"""
        try:
            # Remove original
            if original_path.exists():
                original_path.unlink()
            
            # Create symlink
            original_path.symlink_to(master_path)
            
            return True, "Symlink created"
        except Exception as e:
            return False, f"Error creating symlink: {e}"
    
    def sync_to_original(self, master_path: Path) -> tuple[bool, str]:
        """Sync master copy to original location"""
        try:
            master_path_str = str(master_path)
            if master_path_str not in self.mapping['configs']:
                return False, "Not in mapping"
            
            original_path = Path(self.mapping['configs'][master_path_str]['original'])
            
            # Copy from master to original
            shutil.copy2(master_path, original_path)
            
            return True, f"Synced to {original_path}"
        except Exception as e:
            return False, f"Error: {e}"
    
    def sync_all(self) -> Dict[str, tuple[bool, str]]:
        """Sync all configs to their original locations"""
        results = {}
        
        for master_path_str in self.mapping['configs']:
            master_path = Path(master_path_str)
            if master_path.exists():
                success, msg = self.sync_to_original(master_path)
                results[master_path_str] = (success, msg)
        
        return results
    
    def init_git(self) -> tuple[bool, str]:
        """Initialize git repository for version control"""
        try:
            repo_dir = self.master_dir.parent
            
            if not (repo_dir / '.git').exists():
                subprocess.run(['git', 'init'], cwd=repo_dir, check=True, capture_output=True)
                subprocess.run(['git', 'add', '.'], cwd=repo_dir, check=True, capture_output=True)
                subprocess.run(['git', 'commit', '-m', 'Initial config import'], 
                             cwd=repo_dir, check=True, capture_output=True)
                return True, "Git repository initialized"
            else:
                return True, "Git repository already exists"
        except Exception as e:
            return False, f"Error: {e}"
    
    def commit_changes(self, message: str = None) -> tuple[bool, str]:
        """Commit changes to git"""
        try:
            repo_dir = self.master_dir.parent
            msg = message or f"Config update {datetime.now().isoformat()}"
            
            subprocess.run(['git', 'add', '.'], cwd=repo_dir, check=True, capture_output=True)
            result = subprocess.run(['git', 'commit', '-m', msg], 
                                  cwd=repo_dir, capture_output=True, text=True)
            
            if result.returncode == 0:
                return True, "Changes committed"
            else:
                return True, "No changes to commit"
        except Exception as e:
            return False, f"Error: {e}"


# ============================================================================
# CLI INTERFACE
# ============================================================================

def print_results(results: List[Dict], summary: Dict):
    """Pretty print scan results"""
    print("\n" + "="*70)
    print("CONFIG SCAN RESULTS")
    print("="*70)
    
    # Summary
    print(f"\n📊 Summary:")
    print(f"   Total configs found: {summary['total']}")
    print(f"   Recommended for import: {summary['recommended']}")
    print(f"   Sensitive files: {summary['sensitive']}")
    print(f"   Total size: {summary['total_size_mb']:.1f} MB")
    
    print(f"\n📁 By Category:")
    for cat, count in summary['categories'].items():
        print(f"   {cat}: {count}")
    
    # List configs
    print(f"\n📋 Configurations Found:\n")
    
    for i, item in enumerate(results, 1):
        icon = "🔒" if item['sensitive'] else "📄"
        rec = "✓" if item['recommend_import'] else " "
        
        print(f"[{rec}] {icon} {item['path']}")
        print(f"    Category: {item['category']} | "
              f"Size: {item['size']/1024:.1f}KB | "
              f"Modified: {item['modified'].strftime('%Y-%m-%d %H:%M') if item['modified'] else 'unknown'}")
        
        if item['sensitive']:
            print(f"    ⚠️  Contains sensitive data")
        
        if i < len(results):
            print()


def main():
    """Main CLI interface"""
    print("""
╔═══════════════════════════════════════════════════════════════╗
║           CONFIG CENTRALIZER - Scanner & Manager              ║
║  Find all your configs and centralize them in one place       ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    
    # Scan
    scanner = ConfigScanner()
    results = scanner.scan()
    summary = scanner.get_summary()
    
    # Print results
    print_results(results, summary)
    
    # Ask about import
    print("\n" + "="*70)
    print("IMPORT OPTIONS")
    print("="*70)
    
    response = input("\nImport recommended configs? (y/n/selective): ").lower()
    
    if response in ['y', 'yes']:
        # Import all recommended
        centralizer = ConfigCentralizer()
        print("\n📥 Importing configs...")
        
        imported = 0
        for item in results:
            if item['recommend_import']:
                success, msg = centralizer.import_config(item)
                if success:
                    print(f"  ✓ {item['path'].name}")
                    imported += 1
                else:
                    print(f"  ✗ {item['path'].name}: {msg}")
        
        print(f"\n✓ Imported {imported} configurations")
        print(f"   Master directory: {centralizer.master_dir}")
        
        # Init git
        if input("\nInitialize git for version control? (y/n): ").lower() in ['y', 'yes']:
            success, msg = centralizer.init_git()
            print(f"   {msg}")
        
        print("\n💡 Next steps:")
        print(f"   1. Review configs in: {centralizer.master_dir}")
        print(f"   2. Edit centralized configs as needed")
        print(f"   3. Run 'sync-configs' to deploy changes")
        
    elif response == 'selective':
        print("\n📝 Selective import not yet implemented in CLI")
        print("   Use the GUI widget for interactive selection")
    
    else:
        print("\n   No configs imported.")
        print(f"   Run again anytime to import configs")


if __name__ == '__main__':
    main()

