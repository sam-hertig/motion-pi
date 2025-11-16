# Raspberry Pi PIR Motion Sensor + Web Dashboard + Cloudflare Tunnel

A fully remote, globally accessible motion-detection system using:

- Raspberry Pi Zero 2 WH
- PIR sensor (HC-SR501)
- Flask web server
- Cloudflare Tunnel
- Custom domain: **`motion.example.com`**
- Automatic startup using `systemd`

This project lets you view the **last motion detected timestamp** from anywhere in the world with **zero port-forwarding** and strong Cloudflare security.

## Table of Contents

1. Overview
2. Hardware
3. Wiring
4. Software Components
5. Cloudflare Configuration
6. Boot Sequence
7. Testing
8. Troubleshooting
9. Future Improvements

## Overview

This project runs a PIR motion sensor on a Raspberry Pi Zero 2 WH and serves a small dashboard showing the **last motion detection** timestamp via a Flask web server on port 8080.

A Cloudflare Tunnel maps:

```
https://motion.example.com → http://localhost:8080
```

Both the Flask server and the tunnel run automatically on boot using `systemd`.

## Hardware

- Raspberry Pi Zero 2 WH
- HC-SR501 PIR motion sensor
- 3× female-to-female jumper wires
- Micro-USB OTG cable
- 5V power supply

## Wiring

| PIR Pin | Raspberry Pi Pin | Function |
|--------|-------------------|----------|
| VCC    | Pin 2             | 5V Power |
| GND    | Pin 6             | Ground   |
| OUT    | Pin 11            | GPIO17   |

## Software Components

### motion_web.py

Location: `/home/shertig/motion_web.py`

Purpose: Motion detection + Flask web server on port 8080

### motion.service

Location: `/etc/systemd/system/motion.service`

### Cloudflare Tunnel Credentials

Stored in `/etc/cloudflared/<UUID>.json`

### config.yml

Location: `/etc/cloudflared/config.yml`

### cloudflared.service

Installed with:

```
sudo cloudflared service install
```

## Cloudflare Configuration

DNS entry:

```
CNAME motion.example.com → <tunnel-id>.cfargotunnel.com
```

## Boot Sequence

1. cloudflared.service starts
2. motion.service starts
3. Public dashboard available at `https://motion.example.com`

## Testing

```
sudo systemctl status motion
sudo systemctl status cloudflared
```

## Restarting the motion service after updating the script

1. Update `motion_web.py` on the Raspberry
2. Run `sudo systemctl restart motion` in the terminal on the Raspberry


## Troubleshooting

Check logs:

```
sudo journalctl -u cloudflared -n 50
sudo journalctl -u motion -n 50
```
