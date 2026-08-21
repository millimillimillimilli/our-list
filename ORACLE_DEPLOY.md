# Deploy for free on Oracle Cloud

Oracle Cloud's "Always Free" tier is, as far as anyone can tell in 2026, the
last mainstream cloud platform that gives you a real, always-on server with
persistent storage and never charges you a cent — no trial period, no card
that starts getting billed later. The tradeoff for that is it's a real server
you set up yourself (15-20 minutes, one time), not a single click like the
Render button. This guide walks through the whole thing.

One known rough edge up front: Oracle's free compute instances are popular,
and creating one sometimes fails with an "Out of host capacity" error in your
chosen region. If that happens, just try a different **Availability Domain**
(there's usually 2-3 to pick from) or try again a bit later — it's a known
quirk, not something wrong with your account.

## 1. Create your account

Go to [oracle.com/cloud/free](https://www.oracle.com/cloud/free/) and sign
up. It asks for a card for identity verification, but the Always Free
resources genuinely never bill it — Oracle's own FAQ covers this if you want
to double-check before entering anything.

## 2. Create the free compute instance

From the Oracle Cloud Console (after logging in):

1. Hamburger menu → **Compute** → **Instances** → **Create Instance**
2. **Name**: anything, e.g. `our-list`
3. **Image and shape** → Edit → under "Image" pick **Canonical Ubuntu**
   (22.04 or newer) → under "Shape" pick **Ampere (Arm-based) → VM.Standard.A1.Flex**,
   and set it to 2 OCPUs / 12GB RAM (the current free allowance). If that
   shape is greyed out or fails with a capacity error, the AMD
   `VM.Standard.E2.1.Micro` shape (also free, smaller) is the fallback.
4. Under **Networking**, leave it creating a new VCN — this also gives you a
   public IP automatically.
5. Under **Add SSH keys**, choose "Generate a key pair for me" and **download
   the private key** — you'll need it to log in. Keep it somewhere safe.
6. **Create**. It takes a minute or two to boot.

Once it's running, copy the **Public IP address** shown on the instance
page — you'll use it for everything below.

## 3. Open the ports it needs

Oracle blocks incoming traffic by default at *two* separate layers — this is
the single most common thing people get stuck on, so don't skip either one.

**a) The cloud firewall (Security List):**

1. On your instance's page, click the link under "Virtual cloud network" →
   click into the **Subnet** → click the **Default Security List**
2. **Add Ingress Rules** → add one for port `80` and one for `443`
   (source CIDR `0.0.0.0/0`, protocol TCP) so the app is reachable over
   plain HTTP and HTTPS

**b) The OS firewall, once you're logged into the machine (step 4 below):**

```bash
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

## 4. Log in and install Docker

```bash
chmod 600 /path/to/the-key-you-downloaded.key
ssh -i /path/to/the-key-you-downloaded.key ubuntu@<your-instance-public-ip>
```

Then, on the server:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
```

## 5. Get the app and configure it

```bash
git clone https://github.com/millimillimillimilli/our-list.git
cd our-list
echo "APP_PASSWORD=choose-a-real-password-here" > .env
```

Pick a real password for `APP_PASSWORD` — anyone with your server's address
can otherwise open the app.

## 6. Add HTTPS (recommended, not optional)

Since people will type their household password into this app, it shouldn't
run over plain HTTP. [Caddy](https://caddyserver.com) gets you free,
automatic HTTPS in about two minutes, and it's the one piece not already in
this repo:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

If you have a domain and pointed it at your instance's public IP (an `A`
record), tell Caddy about it:

```bash
echo "yourdomain.com {
    reverse_proxy localhost:8123
}" | sudo tee /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

No domain? Caddy can still serve HTTPS on the bare IP with a self-signed
certificate (your browser will show a one-time warning to accept):

```bash
echo ":443 {
    tls internal
    reverse_proxy localhost:8123
}" | sudo tee /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

## 7. Run it

```bash
docker compose up -d --build
```

Visit `https://yourdomain.com` (or `https://<instance-ip>`) and you should
see the app. From here, everything in the main [README](README.md) applies —
installing it to a phone's home screen, ntfy notifications, etc.

## Keeping it updated

```bash
cd ~/our-list
git pull
docker compose up -d --build
```

Your data (the SQLite database and uploaded photos) lives in a Docker
volume, so pulling and rebuilding never touches it.
