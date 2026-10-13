import { pathToFileURL } from 'node:url';
import path from 'node:path';

process.env.MOCK_MODE = 'standalone';

const root = process.cwd();
const configUrl = pathToFileURL(path.join(root, 'src', 'config.js')).href;
const indexUrl = pathToFileURL(path.join(root, 'src', 'index.js')).href;

const { config } = await import(configUrl);
config.mode = 'standalone';
config.streamingBindIp = '127.0.0.1';
config.ports.control = 27100;
config.ports.mediaHttp = 27101;
config.ports.zlm = 8898;
config.ports.rtsp = 8654;
config.ports.rtc = 8010;

await import(indexUrl);
