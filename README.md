# DRID Modules – Git & GitHub Collaboration Guide

This section explains **how we collaborate** on this project using Git and GitHub, and then gives **step-by-step instructions** for macOS and Windows.

---

## 1. Overall Workflow (for everyone)

We use **Git** for version control and **GitHub** as the shared remote repository.

1. **Central repository**
   - The main project lives in a GitHub repository.
   - All teammates are added as **collaborators**.

2. **Local clones**
   - Each teammate clones the repository once to their own computer.
   - All work is done in the local copy; changes are synchronized with GitHub.

3. **Branching model**
   - `main`: stable branch, should always be in a working state.
   - Feature branches: for each task/bug, create a new branch, e.g.  
     `feature/flask-http-server`, `bugfix/serial-timeout`.

4. **Development loop**
   1. Update local `main` from GitHub.
   2. Create a new branch from `main` for your task.
   3. Edit and test code locally.
   4. Commit changes with clear messages.
   5. Push the branch to GitHub.
   6. Open a **Pull Request (PR)** from your branch into `main`.
   7. Another teammate reviews and merges the PR.

5. **Communication on GitHub**
   - **Issues**: tasks, bugs, feature requests.
   - **Pull Requests**: code review, discussion about changes.
   - **Commit history**: record of what changed, when, and by whom.

---

## 2. Requirements (for all teammates)

Before following the OS-specific steps, everyone needs:

- A **GitHub account**.
- To be added as a **collaborator** on the repository.
- **Git** installed on their machine.
- Basic familiarity with a terminal (macOS Terminal or Git Bash on Windows).

The repository URL will be provided by the owner, for example:

- SSH (recommended):  
  `git@github.com:<OWNER_USERNAME>/<REPO_NAME>.git`
- HTTPS:  
  `https://github.com/<OWNER_USERNAME>/<REPO_NAME>.git`

Replace `<OWNER_USERNAME>` and `<REPO_NAME>` with the actual values.

---

## 3. macOS Instructions

### 3.1 One-time Setup on macOS

1. **Open Terminal**

   - Press `Command + Space`, type `Terminal`, press Enter.

2. **Check Git**

   ```bash
   git --version
   ```
   - If macOS asks to install developer tools, accept and follow the prompts.

3. **Configure Git identity (only once)**

   ```bash
   git config --global user.name "Your Real Name"
   git config --global user.email "your_email@example.com"
   ```

4. **Set up SSH access to GitHub (recommended)**

   Generate an SSH key (skip if you already have one):

   ```bash
   ssh-keygen -t ed25519 -C "your_email@example.com"
   ```

   - Press Enter to accept the default location.
   - Optionally press Enter again to leave the passphrase empty.

   Show and copy the public key:

   ```bash
   cat ~/.ssh/id_ed25519.pub
   ```

   - Copy the entire line starting with `ssh-ed25519`.

   Add the key to GitHub:

   - GitHub → profile icon → **Settings**  
   - **SSH and GPG keys** → **New SSH key**  
   - Title: e.g. `MacBook`  
   - Paste the key → **Add SSH key**

5. **Clone the repository**

   Choose a folder for projects, e.g. `~/Projects`:

   ```bash
   mkdir -p ~/Projects
   cd ~/Projects
   git clone git@github.com:<OWNER_USERNAME>/<REPO_NAME>.git
   cd <REPO_NAME>
   ```

---

### 3.2 Daily Development Workflow on macOS

From inside the project directory:

```bash
cd ~/Projects/<REPO_NAME>
```

1. **Update local `main`**

   ```bash
   git checkout main
   git pull origin main
   ```

2. **Create a feature branch**

   ```bash
   git checkout -b feature/short-description-of-task
   ```

3. **Edit and test code**
   - Use your editor/IDE (VS Code, PyCharm, etc.).

4. **Check changes**

   ```bash
   git status
   ```

5. **Stage and commit**

   ```bash
   git add .
   git commit -m "Short, clear summary of your changes"
   ```

6. **Push the branch to GitHub**

   ```bash
   git push -u origin feature/short-description-of-task
   ```

7. **Open a Pull Request**

   - Go to the repository on GitHub.
   - Create a PR from your branch into `main`.
   - Describe the changes, request review, and iterate if needed.

---

## 4. Windows Instructions

On Windows we use **Git for Windows**, which provides **Git Bash** (a terminal similar to macOS/Linux).

### 4.1 One-time Setup on Windows

1. **Install Git for Windows**

   - Download from the official “Git for Windows” website.
   - Run the installer and accept the default options (including **Git Bash**).

2. **Open Git Bash**

   - Right-click in any folder → **Git Bash Here**,  
     or launch **Git Bash** from the Start menu.

3. **Configure Git identity (only once)**

   ```bash
   git config --global user.name "Your Real Name"
   git config --global user.email "your_email@example.com"
   ```

4. **Set up SSH access to GitHub (recommended)**

   Generate an SSH key:

   ```bash
   ssh-keygen -t ed25519 -C "your_email@example.com"
   ```

   - Accept the default location.
   - Optional passphrase (can be empty).

   Show and copy the public key:

   ```bash
   cat ~/.ssh/id_ed25519.pub
   ```

   - Right-click → Mark → select the full line → press Enter to copy.

   Add the key to GitHub:

   - GitHub → profile icon → **Settings**  
   - **SSH and GPG keys** → **New SSH key**  
   - Title: e.g. `Windows PC`  
   - Paste the key → **Add SSH key**

5. **Clone the repository**

   Choose a folder, e.g. `C:\Projects`:

   ```bash
   cd /c
   mkdir -p Projects
   cd /c/Projects
   git clone git@github.com:<OWNER_USERNAME>/<REPO_NAME>.git
   cd <REPO_NAME>
   ```

---

### 4.2 Daily Development Workflow on Windows

In Git Bash, from the project directory:

```bash
cd /c/Projects/<REPO_NAME>
```

1. **Update local `main`**

   ```bash
   git checkout main
   git pull origin main
   ```

2. **Create a feature branch**

   ```bash
   git checkout -b feature/short-description-of-task
   ```

3. **Edit and test code**
   - Use any editor/IDE (VS Code, PyCharm, etc.).

4. **Check changes**

   ```bash
   git status
   ```

5. **Stage and commit**

   ```bash
   git add .
   git commit -m "Short, clear summary of your changes"
   ```

6. **Push the branch to GitHub**

   ```bash
   git push -u origin feature/short-description-of-task
   ```

7. **Open a Pull Request**

   - On GitHub, open a PR from your branch into `main`.
   - Use the PR for code review and discussion.
   - After approval, the PR is merged into `main`.

---
