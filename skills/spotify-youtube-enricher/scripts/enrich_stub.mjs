#!/usr/bin/env node
// spotify-youtube-enricher: stub
// Usage: node enrich_stub.mjs --spotify-id <id> --youtube-id <id>

const args = process.argv.slice(2);
console.log('spotify-youtube-enricher: stub run with', args.join(' '));
console.log('Output schema: {spotify:{id,monthly_listeners,top_tracks[]}, youtube:{channelId,subscribers,topVideos[]}}');

