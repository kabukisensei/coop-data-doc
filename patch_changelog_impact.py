import datetime
with open('CHANGELOG.md', 'r') as f:
    lines = f.readlines()

new_content = []
for line in lines:
    if line.startswith('## [0.6.0]'):
        new_content.append(f'## [0.7.0] - {datetime.datetime.now().strftime("%Y-%m-%d")}\n')
        new_content.append('### Added\n')
        new_content.append('- `impact` command to compute change-impact diff against a git baseline (PR blast radius) (issue #54)\n\n')
        new_content.append(line)
    else:
        new_content.append(line)

with open('CHANGELOG.md', 'w') as f:
    f.writelines(new_content)
