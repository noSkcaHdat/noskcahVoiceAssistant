# noskcahVoice

Small local project using VOSK speech recognition model.

Notes:
- The `vosk-model-small-en-us-0.15/` model directory is large and is excluded from Git by `.gitignore`.
- If you want to publish the model, consider using Git LFS or host the model separately and add download instructions.

Quick Git setup (PowerShell):

1) Initialize and make the initial commit

```powershell
cd 'C:\Users\karth\OneDrive\Documents\PythonProjects\noskcahVoice'
# initialize repo (only if not already a git repo)
git init
# create first commit
git add .
git commit -m "Initial commit"
```

2) Create a remote repository (GitHub):
- Option A: Create a new repo on https://github.com (do not initialize with README) then add remote:

```powershell
git remote add origin https://github.com/<your-username>/<repo-name>.git
git branch -M main
git push -u origin main
```

- Option B (if you have GitHub CLI `gh` installed):

```powershell
gh repo create <repo-name> --public --source=. --remote=origin --push
```

3) Large model files / alternatives
- Keep models outside the git repo and add a small script to download them, or use Git LFS for models.
- To setup Git LFS:

```powershell
# one-time install (if not installed) - see https://git-lfs.github.com/
git lfs install
# track model file patterns (example)
git lfs track "vosk-model-small-en-us-0.15/**"
# commit the .gitattributes file created by git lfs track
git add .gitattributes
git commit -m "Track model files with LFS"
```

4) Typical daily workflow
```powershell
# update local code
git add <files>
git commit -m "Describe change"
git push
# pull updates from remote
git pull
```

If you want, I can:
- Initialize the repo here for you (run the git commands). Note: requires your local git installed and will run in your environment.
- Create a small download script for the model and add it to the repo instead of storing model files.
