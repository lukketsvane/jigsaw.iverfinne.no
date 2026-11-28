const select = document.getElementById('family');
const generate = document.getElementById('generate');
const statusText = document.getElementById('status');
const preview = document.getElementById('preview');
let editions = [];

function show(run) {
  const downloads = document.getElementById('downloads');
  downloads.replaceChildren();
  preview.hidden = !run?.preview;
  preview.removeAttribute('src');
  if (!run) return;
  const prefix = '/edition-files/' + encodeURIComponent(run.folder) + '/';
  if (run.preview) preview.src = prefix + encodeURIComponent(run.preview);
  for (const filename of run.files) {
    const link = document.createElement('a');
    link.textContent = filename;
    link.href = prefix + encodeURIComponent(filename);
    link.download = filename;
    downloads.append(link);
  }
}

function selectEdition() {
  const selected = editions.find(item => item.id === select.value);
  document.getElementById('description').textContent = selected.description;
  statusText.textContent = selected.sample ? 'Example files' : '';
  show(selected.sample);
}

select.addEventListener('change', selectEdition);
document.getElementById('edition').addEventListener('submit', async event => {
  event.preventDefault();
  select.disabled = generate.disabled = true;
  statusText.textContent = 'Generating and checking the puzzle…';
  try {
    const response = await fetch('/api/edition', {method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({family:select.value,seed:document.getElementById('seed').valueAsNumber})});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Generation failed.');
    statusText.textContent = 'Generated files';
    show(result);
  } catch (error) { statusText.textContent = error.message; }
  finally { select.disabled = generate.disabled = false; }
});

async function loadEditions() {
  const response = await fetch('/api/editions');
  if (!response.ok) throw Error('Could not load the edition catalog.');
  editions = await response.json();
  for (const item of editions) select.add(new Option(item.name, item.id));
  selectEdition();
  generate.disabled = false;
}
loadEditions().catch(error => { statusText.textContent = error.message; });
