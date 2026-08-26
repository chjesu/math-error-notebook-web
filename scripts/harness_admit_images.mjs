import { Context } from "@deepseek-ai/cordis";
import { admitEncodedImages } from "@deepseek-ai/dsh-attachment";
import LocalAttachmentStore from "@deepseek-ai/dsh-attachment-local";

let wire = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) wire += chunk;
const input = JSON.parse(wire);
if (!Array.isArray(input.images)) throw new Error("images must be an array");

const context = new Context();
const store = new LocalAttachmentStore(context, { dshHome: process.env.DSH_HOME });
const references = await admitEncodedImages(store, input.images);
process.stdout.write(JSON.stringify(references));
