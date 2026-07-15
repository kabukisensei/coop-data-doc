import datetime
with open('CHANGELOG.md', 'r') as f:
    lines = f.readlines()

new_content = []
for line in lines:
    if line.startswith('## [0.4.0]'):
        new_content.append(f'## [0.5.0] - {datetime.datetime.now().strftime("%Y-%m-%d")}\n')
        new_content.append('### Added\n')
        new_content.append('- `export` command to generate deterministic CSV data dictionary deliverables (`objects.csv`, `columns.csv`, `measures.csv`, `edges.csv`) (issue #56)\n\n')
        new_content.append(line)
    else:
        new_content.append(line)

with open('CHANGELOG.md', 'w') as f:
    f.writelines(new_content)
