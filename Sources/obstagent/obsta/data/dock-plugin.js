const { Plugin } = require('obsidian');
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

function readJson(file) {
    try {
        return JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch (error) {
        if (error.code === 'ENOENT') return {};
        throw error;
    }
}

module.exports = class ObstaDockIcon extends Plugin {
    async onload() {
        if (process.platform !== 'darwin') return;
        const { app } = require('electron').remote;
        const { statePath } = await this.loadData() || {};
        if (typeof statePath !== 'string' || !path.isAbsolute(statePath)) return;

        const profile = app.getPath('userData');
        const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'obsta-dock-'));
        const fruit = path.join(directory, 'fruit.png');
        const standard = path.join(directory, 'standard.png');

        const restore = () => {
            if (!this.applied) return;
            const current = readJson(path.join(profile, 'obsidian.json')).icon;
            const original = current === 'obsta-app-icon.png'
                ? readJson(path.join(profile, 'obsta-app-icon.json')).original
                : current;
            const custom = typeof original === 'string' && path.basename(original) === original
                ? path.join(profile, original)
                : null;
            if (custom && fs.existsSync(custom)) {
                app.dock.setIcon(custom);
            } else {
                if (!fs.existsSync(standard)) {
                    execFileSync('/usr/bin/sips', [
                        '-s', 'format', 'png', path.join(process.resourcesPath, 'icon.icns'), '--out', standard,
                    ], { timeout: 5000 });
                }
                app.dock.setIcon(standard);
            }
            this.applied = null;
        };

        const refresh = (force = false) => {
            try {
                const { activation_id, emoji } = readJson(statePath);
                if (typeof activation_id !== 'string' || typeof emoji !== 'string' || !emoji) {
                    restore();
                    return;
                }
                if (!force && this.applied === activation_id) return;

                const canvas = document.createElement('canvas');
                canvas.width = canvas.height = 256;
                const context = canvas.getContext('2d');
                context.font = '180px "Apple Color Emoji"';
                context.textAlign = 'center';
                context.textBaseline = 'middle';
                context.fillText(emoji, 128, 128, 236);
                const png = canvas.toDataURL('image/png').split(',')[1];
                fs.writeFileSync(fruit, Buffer.from(png, 'base64'));
                app.dock.setIcon(fruit);
                this.applied = activation_id;
            } catch (error) {
                console.warn('obsta: could not update the Dock icon', error);
            }
        };

        this.register(() => {
            try {
                restore();
            } finally {
                fs.rmSync(directory, { recursive: true, force: true });
            }
        });
        this.registerInterval(window.setInterval(() => refresh(), 500));
        this.registerDomEvent(window, 'focus', () => refresh(true));
        refresh();
    }
};
