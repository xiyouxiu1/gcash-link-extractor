'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const vm = require('node:vm');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const sdkPath = process.env.OPENAI_SENTINEL_SDK_FILE;
if (!sdkPath) throw new Error('OPENAI_SENTINEL_SDK_FILE is missing');

let sdkSource = fs.readFileSync(sdkPath, 'utf8');
sdkSource = sdkSource.replace(/\bvar SentinelSDK=/, 'globalThis.SentinelSDK=');
const singletonPattern = /var ([A-Za-z_$][\w$]*)=new ([A-Za-z_$][\w$]*);/;
sdkSource = sdkSource.replace(
  singletonPattern,
  (statement, name) => `${statement}globalThis.__sentinelInternal=${name};`,
);

function storage() {
  const values = new Map();
  return {
    get length() { return values.size; },
    clear() { values.clear(); },
    getItem(key) { return values.has(String(key)) ? values.get(String(key)) : null; },
    setItem(key, value) { values.set(String(key), String(value)); },
    removeItem(key) { values.delete(String(key)); },
    key(index) { return [...values.keys()][index] ?? null; },
  };
}

function element(tagName) {
  const name = String(tagName || 'div').toUpperCase();
  const children = [];
  return {
    nodeType: 1,
    nodeName: name,
    tagName: name,
    children,
    childNodes: children,
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    parentNode: null,
    src: '',
    id: '',
    className: '',
    innerHTML: '',
    textContent: '',
    appendChild(child) { children.push(child); child.parentNode = this; return child; },
    removeChild(child) {
      const index = children.indexOf(child);
      if (index >= 0) children.splice(index, 1);
      child.parentNode = null;
      return child;
    },
    insertBefore(child) { return this.appendChild(child); },
    cloneNode() { return element(name); },
    setAttribute(key, value) { this[String(key)] = String(value); },
    getAttribute(key) { return this[String(key)] ?? null; },
    hasAttribute(key) { return this[String(key)] != null; },
    removeAttribute(key) { delete this[String(key)]; },
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() { return true; },
    contains() { return false; },
    focus() {},
    blur() {},
    click() {},
    getBoundingClientRect() {
      return { x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 };
    },
  };
}

function canvas() {
  const value = element('canvas');
  value.width = 300;
  value.height = 150;
  value.toDataURL = () => 'data:image/png;base64,';
  value.toBlob = (callback) => callback?.(new Uint8Array());
  value.getContext = (kind) => {
    if (kind === '2d') {
      return {
        canvas: value,
        fillStyle: '', strokeStyle: '', lineWidth: 1, font: '10px sans-serif',
        textAlign: 'start', textBaseline: 'alphabetic', globalAlpha: 1,
        fillRect() {}, clearRect() {}, strokeRect() {}, putImageData() {}, drawImage() {},
        setTransform() {}, resetTransform() {}, save() {}, restore() {}, beginPath() {},
        closePath() {}, moveTo() {}, lineTo() {}, clip() {}, quadraticCurveTo() {},
        bezierCurveTo() {}, arc() {}, arcTo() {}, rect() {}, fill() {}, stroke() {},
        fillText() {}, strokeText() {}, scale() {}, rotate() {}, translate() {},
        measureText() { return { width: 0 }; },
        getImageData() { return { data: new Uint8Array() }; },
        createImageData() { return { data: new Uint8Array() }; },
        createLinearGradient() { return { addColorStop() {} }; },
        createRadialGradient() { return { addColorStop() {} }; },
      };
    }
    if (!['webgl', 'webgl2', 'experimental-webgl'].includes(kind)) return null;
    const debugInfo = { UNMASKED_VENDOR_WEBGL: 0x9245, UNMASKED_RENDERER_WEBGL: 0x9246 };
    return {
      canvas: value,
      VENDOR: 0x1F00,
      RENDERER: 0x1F01,
      getExtension(name) { return name === 'WEBGL_debug_renderer_info' ? debugInfo : null; },
      getParameter(parameter) {
        if (parameter === debugInfo.UNMASKED_VENDOR_WEBGL || parameter === 0x1F00) return 'Google Inc. (Intel)';
        if (parameter === debugInfo.UNMASKED_RENDERER_WEBGL || parameter === 0x1F01) {
          return 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11)';
        }
        return 0;
      },
      getSupportedExtensions() { return ['WEBGL_debug_renderer_info']; },
      createBuffer() { return {}; }, createTexture() { return {}; },
      createShader() { return {}; }, createProgram() { return {}; },
      bindBuffer() {}, bufferData() {}, bindTexture() {}, viewport() {}, clear() {},
      enable() {}, disable() {}, drawArrays() {}, drawElements() {},
    };
  };
  return value;
}

const listeners = new Map();
function on(type, callback) {
  if (typeof callback !== 'function') return;
  const bucket = listeners.get(type) || [];
  bucket.push(callback);
  listeners.set(type, bucket);
}
function off(type, callback) {
  listeners.set(type, (listeners.get(type) || []).filter((item) => item !== callback));
}
async function emit(type, details = {}) {
  const event = {
    type,
    bubbles: true,
    cancelable: true,
    defaultPrevented: false,
    timeStamp: performance.now(),
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() {},
    stopImmediatePropagation() {},
    ...details,
  };
  for (const callback of [...(listeners.get(type) || [])]) {
    try { await callback(event); } catch (_) {}
  }
}

const width = Number(input.screen_width || 1920);
const height = Number(input.screen_height || 1080);
const page = new URL(String(input.page_url || 'https://chatgpt.com/'));
const pageOrigin = String(input.page_origin || page.origin);
const scripts = [];
let iframe = null;
let capturedProof = '';

const documentElement = element('html');
documentElement.clientWidth = width;
documentElement.clientHeight = height;
const body = element('body');
body.appendChild = function appendChild(child) {
  this.children.push(child);
  child.parentNode = this;
  if (child === iframe) {
    setTimeout(() => iframe.__loadListeners.forEach((callback) => callback()), 1);
  }
  return child;
};

const originalDateTimeFormat = Intl.DateTimeFormat;
function DateTimeFormat(locales, options) {
  const formatter = new originalDateTimeFormat(locales, options);
  const original = formatter.resolvedOptions.bind(formatter);
  formatter.resolvedOptions = () => ({ ...original(), timeZone: String(input.timezone || 'UTC') });
  return formatter;
}
Object.setPrototypeOf(DateTimeFormat, originalDateTimeFormat);
DateTimeFormat.prototype = originalDateTimeFormat.prototype;
DateTimeFormat.supportedLocalesOf = originalDateTimeFormat.supportedLocalesOf;

const webCrypto = {
  getRandomValues(array) { crypto.randomFillSync(array); return array; },
  randomUUID: () => crypto.randomUUID(),
  subtle: crypto.webcrypto.subtle,
};

const navigatorValue = {
  userAgent: String(input.user_agent || 'Mozilla/5.0'),
  language: String(input.language || 'en-PH'),
  languages: Array.isArray(input.languages) ? input.languages : ['en-PH', 'en'],
  hardwareConcurrency: Number(input.hardware_concurrency || 8),
  deviceMemory: 8,
  platform: 'Win32',
  vendor: 'Google Inc.',
  maxTouchPoints: 0,
  webdriver: false,
  onLine: true,
  cookieEnabled: true,
  doNotTrack: null,
  appCodeName: 'Mozilla',
  appName: 'Netscape',
  appVersion: '5.0',
  product: 'Gecko',
  productSub: '20030107',
  vendorSub: '',
  connection: { effectiveType: '4g', rtt: 50, downlink: 10, saveData: false },
  plugins: { length: 5 },
  mimeTypes: { length: 2 },
  mediaDevices: { enumerateDevices: async () => [] },
  getBattery: async () => ({ charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1 }),
  sendBeacon: () => true,
  permissions: { query: async () => ({ state: 'prompt' }) },
};

const context = {
  console,
  setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
  Promise, URL, URLSearchParams, Math, Date, JSON, Array, Object, String, Number,
  Boolean, RegExp, Function, Symbol, Reflect, Proxy, Error, TypeError, RangeError,
  ReferenceError, SyntaxError, Map, Set, WeakMap, WeakSet, Int8Array, Uint8Array,
  Uint8ClampedArray, Int16Array, Uint16Array, Int32Array, Uint32Array, Float32Array,
  Float64Array, ArrayBuffer, DataView, TextEncoder, TextDecoder, AbortController,
  AbortSignal, Blob, Intl: { ...Intl, DateTimeFormat },
  btoa: (value) => Buffer.from(String(value || ''), 'binary').toString('base64'),
  atob: (value) => Buffer.from(String(value || ''), 'base64').toString('binary'),
  encodeURIComponent, decodeURIComponent, encodeURI, decodeURI, parseInt, parseFloat,
  isFinite, isNaN, NaN, Infinity, undefined,
  crypto: webCrypto,
  performance: {
    now: () => performance.now(),
    timeOrigin: performance.timeOrigin,
    memory: { jsHeapSizeLimit: 4294967296 },
    getEntriesByType: () => [], getEntriesByName: () => [], mark() {}, measure() {},
  },
  screen: {
    width, height, availWidth: width, availHeight: height, colorDepth: 24, pixelDepth: 24,
    orientation: { type: 'landscape-primary', angle: 0 },
  },
  navigator: navigatorValue,
  history: {
    length: 1, state: null, back() {}, forward() {}, go() {}, pushState() {}, replaceState() {},
  },
  localStorage: storage(),
  sessionStorage: storage(),
  innerWidth: width, innerHeight: height, outerWidth: width, outerHeight: height + 80,
  devicePixelRatio: 1, scrollX: 0, scrollY: 0, pageXOffset: 0, pageYOffset: 0,
  requestAnimationFrame(callback) { setTimeout(callback, 16); return 1; },
  cancelAnimationFrame() {},
  requestIdleCallback(callback) {
    callback?.({ didTimeout: false, timeRemaining: () => 50 });
    return 1;
  },
  cancelIdleCallback() {},
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  matchMedia: (query) => ({
    media: String(query || ''), matches: false, onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
    dispatchEvent() { return false; },
  }),
  Event: class Event { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } },
  CustomEvent: class CustomEvent {
    constructor(type, init = {}) { this.type = type; this.detail = init.detail ?? null; }
  },
  MessageChannel: class MessageChannel {
    constructor() {
      this.port1 = { postMessage() {}, addEventListener() {}, removeEventListener() {}, start() {}, close() {} };
      this.port2 = { postMessage() {}, addEventListener() {}, removeEventListener() {}, start() {}, close() {} };
    }
  },
  chrome: { runtime: {}, app: {} },
  CSS: { supports: () => true },
  indexedDB: { open: () => ({}), deleteDatabase: () => ({}) },
  fetch: async () => { throw new Error('network fetch is disabled in Sentinel VM'); },
  postMessage() {},
  addEventListener: on,
  removeEventListener: off,
  dispatchEvent(event) { emit(event.type, event); return true; },
  origin: pageOrigin,
  location: {
    href: page.href, origin: pageOrigin, protocol: page.protocol, host: page.host,
    hostname: page.hostname, pathname: page.pathname, search: page.search, hash: page.hash,
    assign() {}, replace() {}, reload() {},
  },
};

context.document = {
  readyState: 'complete', hidden: false, visibilityState: 'visible',
  referrer: page.href, URL: page.href, documentURI: page.href,
  location: context.location,
  cookie: `oai-did=${encodeURIComponent(input.device_id || '')}`,
  title: '', characterSet: 'UTF-8', contentType: 'text/html',
  scripts,
  currentScript: { src: String(input.current_script_url || ''), getAttribute: () => null },
  documentElement, body, head: element('head'),
  createElement(tag) {
    const name = String(tag || '').toLowerCase();
    if (name === 'canvas') return canvas();
    if (name === 'iframe') {
      iframe = element('iframe');
      iframe.__loadListeners = [];
      iframe.addEventListener = (type, callback) => {
        if (type === 'load' && typeof callback === 'function') iframe.__loadListeners.push(callback);
      };
      iframe.removeEventListener = () => {};
      iframe.contentWindow = {
        postMessage(message, origin) {
          capturedProof = String(message?.p || '');
          const result = input.action === 'solve'
            ? { cachedChatReq: input.challenge, cachedProof: input.request_p || capturedProof }
            : null;
          const event = {
            source: iframe.contentWindow,
            data: { type: 'response', requestId: message?.requestId, result },
            origin,
          };
          setTimeout(() => (listeners.get('message') || []).forEach((callback) => callback(event)), 0);
        },
      };
      return iframe;
    }
    const value = element(name);
    if (name === 'script') scripts.push(value);
    return value;
  },
  createElementNS(_namespace, tag) { return this.createElement(tag); },
  createDocumentFragment: () => element('fragment'),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
  createComment: (text) => ({ nodeType: 8, textContent: String(text) }),
  querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
  getElementsByTagName: (tag) => String(tag).toLowerCase() === 'script' ? scripts : [],
  getElementsByClassName: () => [],
  addEventListener: on, removeEventListener: off,
  dispatchEvent(event) { emit(event.type, event); return true; },
};

context.window = context;
context.globalThis = context;
context.self = context;
context.top = context;
context.parent = context;
vm.createContext(context);
vm.runInContext(sdkSource, context, { timeout: 10000 });

function randomInteger(min, max) {
  return min + Math.floor(Math.random() * (max - min + 1));
}

async function simulateBehavior(duration) {
  const started = Date.now();
  let x = randomInteger(260, 420);
  let y = randomInteger(180, 300);
  for (let index = 0; index < randomInteger(12, 16); index += 1) {
    const dx = randomInteger(5, 18);
    const dy = randomInteger(-4, 12);
    x += dx;
    y += dy;
    await new Promise((resolve) => setTimeout(resolve, randomInteger(70, 145)));
    await emit('pointermove', { clientX: x, clientY: y, screenX: x, screenY: y, movementX: dx, movementY: dy });
  }
  await emit('click', { clientX: x, clientY: y, screenX: x, screenY: y, button: 0 });
  for (let index = 0; index < 3; index += 1) {
    context.scrollY += randomInteger(35, 120);
    context.pageYOffset = context.scrollY;
    await emit('scroll', { scrollY: context.scrollY });
  }
  await emit('wheel', { deltaX: 0, deltaY: randomInteger(70, 140), clientX: x, clientY: y });
  for (const key of ['L', 'u', 'Tab']) await emit('keydown', { key, code: key === 'Tab' ? 'Tab' : `Key${key}` });
  const remaining = Math.max(0, Number(duration || 0) - (Date.now() - started));
  if (remaining) await new Promise((resolve) => setTimeout(resolve, remaining));
}

function timeout(promise, milliseconds, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(`${label} timeout`)), milliseconds)),
  ]);
}

(async () => {
  const flow = String(input.flow || 'chatgpt_checkout');
  if (input.action === 'requirements') {
    try {
      await timeout(context.SentinelSDK.init(flow), 8000, 'init');
      if (capturedProof) return process.stdout.write(JSON.stringify({ request_p: capturedProof }));
    } catch (_) {}
    const proof = await context.__sentinelInternal.getRequirementsToken();
    return process.stdout.write(JSON.stringify({ request_p: proof }));
  }
  if (input.action === 'solve') {
    let tokenError = '';
    try {
      const token = await timeout(context.SentinelSDK.token(flow), 10000, 'token');
      if (token) {
        await simulateBehavior(Number(input.behavior_duration_ms || 4200));
        let soToken = '';
        try {
          soToken = await timeout(context.SentinelSDK.sessionObserverToken(flow), 6000, 'observer');
        } catch (_) {}
        return process.stdout.write(JSON.stringify({ token, so_token: soToken || '' }));
      }
    } catch (error) {
      tokenError = error?.message || String(error);
    }
    return process.stdout.write(JSON.stringify({ token: '', so_token: '', sdk_token_error: tokenError || 'empty token' }));
  }
  throw new Error(`unsupported action: ${input.action}`);
})().catch((error) => {
  process.stderr.write(String(error?.stack || error));
  process.exit(1);
});
