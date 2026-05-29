# First thing — snapshot checkpoint
exit
# Confirm what we're working with
python3 --version
uname -r
df -h
whoami
multipass stop phoenix-dev
multipass snapshot phoenix-dev --name clean-boot
multipass start phoenix-dev
multipass snapshot phoenix-dev --name clean-boot
snapshot
multipass snapshot phoenix-dev --name clean-boot
multipass shell phoenix-devmultipass: command not found
ubuntu@phoenix-dev:~$ multipass snapshot phoenix-dev --name clean-boot
ubuntu@phoenix-dev:~$ snapshot
Command 'snapshot' not found, did you mean:
Try: sudo apt install <deb name>
ubuntu@phoenix-dev:~$ multipass snapshot phoenix-dev --name clean-boot
ubuntu@phoenix-dev:~$ multipass shell phoenix-dev
multipass stop phoenix-dev
multipass snapshot phoenix-dev --name clean-boot
exit
ls -la *.py
sudo apt update
sudo apt --upgrade
sudo apt upgrade
nano ~/THEMOMENT.txt
mkdir ~/HLK
cd ~/HLK
git init
echo "# Helix Lightning Kernel" > README.md
echo "Private — Phoenix DevOps LLC" >> README.md
git add .
git commit -m "the moment — May 28 2026"
cd ~/HLK
git log --oneline
git config --global "jwl247"

git config --global user.name "jwl247"
git config --global user.email "jerry.leftwich1@gmail.com"
mkdir ~/HLK
cd ~/HLK
git init
echo "# Helix Lightning Kernel" > README.md
echo "Private — Phoenix DevOps LLC" >> README.md
git add .
git commit -m "the moment — May 28 2026"
cd ~/HLK
git log --oneline
exit
ls -la *.py
python3 -c "import mmap, socket, struct, threading, signal; print('all good')"
multipass shell phoenix-dev
python3 helix_i.py
ls
sudo nano exit
exit
