#!/usr/bin/env python3
"""
Daily Memory Sync — Consolidate daily facts into long-term memory
Runs 6:00 AM ET (11:00 AM UTC) daily M-F
Pulls facts from yesterday's daily log → merges into MEMORY.md
"""

import json
from pathlib import Path
from datetime import datetime, timedelta

class DailyMemorySync:
    def __init__(self):
        self.workspace = Path.home() / ".openclaw/workspace"
        self.today = datetime.now().strftime('%Y-%m-%d')
        self.yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    
    def extract_facts_from_daily_log(self):
        """Pull all MEMORY_UPDATE sections from yesterday's log"""
        daily_log = self.workspace / f"memory/{self.yesterday}.md"
        
        if not daily_log.exists():
            print(f"ℹ️  No daily log found for {self.yesterday}")
            return []
        
        content = daily_log.read_text()
        lines = content.split('\n')
        
        facts = []
        in_memory_section = False
        current_fact = []
        
        for line in lines:
            if '## MEMORY_UPDATE' in line or '### MEMORY_UPDATE' in line:
                in_memory_section = True
                continue
            
            if in_memory_section:
                if line.startswith('##') and 'MEMORY' not in line:
                    # End of memory section
                    if current_fact:
                        facts.append('\n'.join(current_fact).strip())
                    in_memory_section = False
                else:
                    current_fact.append(line)
        
        if current_fact:
            facts.append('\n'.join(current_fact).strip())
        
        return [f for f in facts if f]
    
    def merge_into_memory(self, facts):
        """Merge facts into MEMORY.md under appropriate section"""
        memory_file = self.workspace / "MEMORY.md"
        
        if not memory_file.exists():
            print("❌ MEMORY.md not found")
            return False
        
        content = memory_file.read_text()
        
        if not facts:
            print(f"✅ No facts to merge ({self.yesterday})")
            return True
        
        # Find insertion point: after "## Updates" or before next major section
        lines = content.split('\n')
        insert_idx = None
        
        for i, line in enumerate(lines):
            if line.startswith('## Updates'):
                # Insert after this section marker
                insert_idx = i + 1
                break
        
        if insert_idx is None:
            # Create Updates section if it doesn't exist
            # Insert after Portfolio Authority section
            for i, line in enumerate(lines):
                if 'Portfolio Authority' in line:
                    insert_idx = i + 10  # Safe distance
                    break
        
        if insert_idx is None:
            print("⚠️  Could not find insertion point in MEMORY.md")
            return False
        
        # Add date marker and facts
        update_block = [
            f"\n**[{self.yesterday}]**",
        ]
        for fact in facts:
            update_block.append(f"- {fact}")
        
        lines.insert(insert_idx, '\n'.join(update_block))
        
        # Write back
        memory_file.write_text('\n'.join(lines))
        
        print(f"✅ Merged {len(facts)} fact(s) into MEMORY.md")
        return True
    
    def deduplicate_memory(self):
        """Remove obvious duplicates from MEMORY.md"""
        memory_file = self.workspace / "MEMORY.md"
        content = memory_file.read_text()
        
        # Simple dedup: if same fact appears twice, keep first, remove second
        lines = content.split('\n')
        seen = set()
        deduped = []
        
        for line in lines:
            # Only dedup fact lines (start with -)
            if line.strip().startswith('- '):
                if line in seen:
                    continue
                seen.add(line)
            deduped.append(line)
        
        if len(deduped) < len(lines):
            memory_file.write_text('\n'.join(deduped))
            print(f"✅ Removed {len(lines) - len(deduped)} duplicate line(s)")
        else:
            print("✅ No duplicates found")
    
    def run(self):
        """Execute daily sync"""
        print(f"\n{'=' * 80}")
        print(f"DAILY MEMORY SYNC — {self.today}")
        print(f"{'=' * 80}")
        
        facts = self.extract_facts_from_daily_log()
        
        if facts:
            print(f"\n📝 Found {len(facts)} fact(s) to merge:")
            for fact in facts[:3]:
                print(f"  - {fact[:60]}...")
            if len(facts) > 3:
                print(f"  ... and {len(facts) - 3} more")
        
        merged = self.merge_into_memory(facts)
        
        if merged:
            self.deduplicate_memory()
            print(f"\n✅ Daily memory sync complete")
            return 0
        else:
            print(f"\n⚠️  Daily memory sync had issues")
            return 1


if __name__ == "__main__":
    import sys
    sync = DailyMemorySync()
    exit_code = sync.run()
    sys.exit(exit_code)
