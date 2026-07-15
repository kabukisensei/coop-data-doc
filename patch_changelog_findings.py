import datetime
with open('CHANGELOG.md', 'r') as f:
    lines = f.readlines()

new_content = []
for line in lines:
    if line.startswith('## [0.5.0]'):
        new_content.append(f'## [0.6.0] - {datetime.datetime.now().strftime("%Y-%m-%d")}\n')
        new_content.append('### Added\n')
        new_content.append('- `findings` command to emit diagnostics as a standard review-findings envelope (issue #55)\n')
        new_content.append('- Support for composing `coop-data-doc` envelope files (schema version 1) via `--reviews` (issue #55)\n\n')
        new_content.append(line)
    else:
        new_content.append(line)

with open('CHANGELOG.md', 'w') as f:
    f.writelines(new_content)
