import os
import argparse
import time
import json
import base64
import re
import urllib.request

import torch
import torchaudio
import music_tag
import gradio as gr
import gradio.utils

from datetime import datetime

from fastapi import FastAPI

from tortoise.api import TextToSpeech
from tortoise.utils.audio import load_audio, load_voice, load_voices
from tortoise.utils.text import split_and_recombine_text

def generate(text, delimiter, emotion, prompt, voice, mic_audio, seed, candidates, num_autoregressive_samples, diffusion_iterations, temperature, diffusion_sampler, breathing_room, cvvp_weight, experimentals, progress=gr.Progress(track_tqdm=True)):
    if voice != "microphone":
        voices = [voice]
    else:
        voices = []

    if voice == "microphone":
        if mic_audio is None:
            raise gr.Error("Please provide audio from mic when choosing `microphone` as a voice input")
        mic = load_audio(mic_audio, tts.input_sample_rate)
        voice_samples, conditioning_latents = [mic], None
    else:
        progress(0, desc="Loading voice...")
        voice_samples, conditioning_latents = load_voice(voice)

    if voice_samples is not None:
        sample_voice = voice_samples[0]
        conditioning_latents = tts.get_conditioning_latents(voice_samples, return_mels=not args.latents_lean_and_mean, progress=progress, max_chunk_size=args.cond_latent_max_chunk_size)
        if len(conditioning_latents) == 4:
            conditioning_latents = (conditioning_latents[0], conditioning_latents[1], conditioning_latents[2], None)
            
        if voice != "microphone":
            torch.save(conditioning_latents, f'./tortoise/voices/{voice}/cond_latents.pth')
        voice_samples = None
    else:
        sample_voice = None

    if seed == 0:
        seed = None

    if conditioning_latents is not None and len(conditioning_latents) == 2 and cvvp_weight > 0:
        print("Requesting weighing against CVVP weight, but voice latents are missing some extra data. Please regenerate your voice latents.")
        cvvp_weight = 0

    start_time = time.time()

    settings = {
        'temperature': temperature, 'length_penalty': 1.0, 'repetition_penalty': 2.0,
        'top_p': .8,
        'cond_free_k': 2.0, 'diffusion_temperature': 1.0,

        'num_autoregressive_samples': num_autoregressive_samples,
        'sample_batch_size': args.sample_batch_size,
        'diffusion_iterations': diffusion_iterations,

        'voice_samples': voice_samples,
        'conditioning_latents': conditioning_latents,
        'use_deterministic_seed': seed,
        'return_deterministic_state': True,
        'k': candidates,
        'diffusion_sampler': diffusion_sampler,
        'breathing_room': breathing_room,
        'progress': progress,
        'half_p': "Half Precision" in experimentals,
        'cond_free': "Conditioning-Free" in experimentals,
        'cvvp_amount': cvvp_weight,
    }

    if delimiter == "\\n":
        delimiter = "\n"

    if delimiter != "" and delimiter in text:
        texts = text.split(delimiter)
    else:
        texts = split_and_recombine_text(text)
 
 
    timestamp = int(time.time())
    outdir = f"./results/{voice}/{timestamp}/"
 
    os.makedirs(outdir, exist_ok=True)
 

    audio_cache = {}
    for line, cut_text in enumerate(texts):
        if emotion == "Custom":
            if prompt.strip() != "":
                cut_text = f"[{prompt},] {cut_text}"
        else:
            cut_text = f"[I am really {emotion.lower()},] {cut_text}"

        print(f"[{str(line+1)}/{str(len(texts))}] Generating line: {cut_text}")

        gen, additionals = tts.tts(cut_text, **settings )
        seed = additionals[0]
 
        if isinstance(gen, list):
            for j, g in enumerate(gen):
                audio = g.squeeze(0).cpu()
                audio_cache[f"candidate_{j}/result_{line}.wav"] = {
                    'audio': audio,
                    'text': cut_text,
                }

                os.makedirs(f'{outdir}/candidate_{j}', exist_ok=True)
                torchaudio.save(f'{outdir}/candidate_{j}/result_{line}.wav', audio, tts.output_sample_rate)
        else:
            audio = gen.squeeze(0).cpu()
            audio_cache[f"result_{line}.wav"] = {
                'audio': audio,
                'text': cut_text,
            }
            torchaudio.save(f'{outdir}/result_{line}.wav', audio, tts.output_sample_rate)
 
    output_voice = None
    if len(texts) > 1:
        for candidate in range(candidates):
            audio_clips = []
            for line in range(len(texts)):
                if isinstance(gen, list):
                    audio = audio_cache[f'candidate_{candidate}/result_{line}.wav']['audio']
                else:
                    audio = audio_cache[f'result_{line}.wav']['audio']
                audio_clips.append(audio)
            
            audio = torch.cat(audio_clips, dim=-1)
            torchaudio.save(f'{outdir}/combined_{candidate}.wav', audio, tts.output_sample_rate)

            audio = audio.squeeze(0).cpu()
            audio_cache[f'combined_{candidate}.wav'] = {
                'audio': audio,
                'text': cut_text,
            }

            if output_voice is None:
                output_voice = audio
    else:
        if isinstance(gen, list):
            output_voice = gen[0]
        else:
            output_voice = gen
    
    if output_voice is not None:
        output_voice = (tts.output_sample_rate, output_voice.numpy())

    info = {
        'text': text,
        'delimiter': '\\n' if delimiter == "\n" else delimiter,
        'emotion': emotion,
        'prompt': prompt,
        'voice': voice,
        'mic_audio': mic_audio,
        'seed': seed,
        'candidates': candidates,
        'num_autoregressive_samples': num_autoregressive_samples,
        'diffusion_iterations': diffusion_iterations,
        'temperature': temperature,
        'diffusion_sampler': diffusion_sampler,
        'breathing_room': breathing_room,
        'cvvp_weight': cvvp_weight,
        'experimentals': experimentals,
        'time': time.time()-start_time,
    }
    
    with open(f'{outdir}/input.json', 'w', encoding="utf-8") as f:
        f.write(json.dumps(info, indent='\t') )

    if voice is not None and conditioning_latents is not None:
        with open(f'./tortoise/voices/{voice}/cond_latents.pth', 'rb') as f:
            info['latents'] = base64.b64encode(f.read()).decode("ascii")

    if args.embed_output_metadata:
        for path in audio_cache:
            info['text'] = audio_cache[path]['text']

            metadata = music_tag.load_file(f"{outdir}/{path}")
            metadata['lyrics'] = json.dumps(info) 
            metadata.save()
 
    if sample_voice is not None:
        sample_voice = (tts.input_sample_rate, sample_voice.squeeze().cpu().numpy())
 
    print(f"Generation took {info['time']} seconds, saved to '{outdir}'\n")

    info['seed'] = settings['use_deterministic_seed']
    del info['latents']
    with open(f'./config/generate.json', 'w', encoding="utf-8") as f:
        f.write(json.dumps(info, indent='\t') )

    return (
        sample_voice,
        output_voice, 
        seed
    )

def update_presets(value):
    PRESETS = {
        'Ultra Fast': {'num_autoregressive_samples': 16, 'diffusion_iterations': 30, 'cond_free': False},
        'Fast': {'num_autoregressive_samples': 96, 'diffusion_iterations': 80},
        'Standard': {'num_autoregressive_samples': 256, 'diffusion_iterations': 200},
        'High Quality': {'num_autoregressive_samples': 256, 'diffusion_iterations': 400},
    }
    
    if value in PRESETS:
        preset = PRESETS[value]
        return (gr.update(value=preset['num_autoregressive_samples']), gr.update(value=preset['diffusion_iterations']))
    else:
        return (gr.update(), gr.update())

def read_generate_settings(file, save_latents=True):
    j = None
    latents = None

    if file is not None:
        if hasattr(file, 'name'):
            metadata = music_tag.load_file(file.name)
            if 'lyrics' in metadata:
                j = json.loads(str(metadata['lyrics']))
        elif file[-5:] == ".json":
            with open(file, 'r') as f:
                j = json.load(f)
    
    if 'latents' in j and save_latents:
        latents = base64.b64decode(j['latents'])
        del j['latents']

    if latents and save_latents:
        outdir='./voices/.temp/'
        os.makedirs(outdir, exist_ok=True)
        with open(f'{outdir}/cond_latents.pth', 'wb') as f:
            f.write(latents)
        latents = f'{outdir}/cond_latents.pth'

    return (
        j,
        latents
    )

def import_generate_settings(file="./config/generate.json"):
    settings, _ = read_generate_settings(file, save_latents=False)
    
    if settings is None:
        return None

    return (
        None if 'text' not in settings else settings['text'],
        None if 'delimiter' not in settings else settings['delimiter'],
        None if 'emotion' not in settings else settings['emotion'],
        None if 'prompt' not in settings else settings['prompt'],
        None if 'voice' not in settings else settings['voice'],
        None if 'mic_audio' not in settings else settings['mic_audio'],
        None if 'seed' not in settings else settings['seed'],
        None if 'candidates' not in settings else settings['candidates'],
        None if 'num_autoregressive_samples' not in settings else settings['num_autoregressive_samples'],
        None if 'diffusion_iterations' not in settings else settings['diffusion_iterations'],
        None if 'temperature' not in settings else settings['temperature'],
        None if 'diffusion_sampler' not in settings else settings['diffusion_sampler'],
        None if 'breathing_room' not in settings else settings['breathing_room'],
        None if 'cvvp_weight' not in settings else settings['cvvp_weight'],
        None if 'experimentals' not in settings else settings['experimentals'],
    )

def curl(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Python'})
        conn = urllib.request.urlopen(req)
        data = conn.read()
        data = data.decode()
        data = json.loads(data)
        conn.close()
        return data
    except Exception as e:
        print(e)
        return None

def check_for_updates():
    if not os.path.isfile('./.git/FETCH_HEAD'):
        print("Cannot check for updates: not from a git repo")
        return False

    with open(f'./.git/FETCH_HEAD', 'r', encoding="utf-8") as f:
        head = f.read()
    
    match = re.findall(r"^([a-f0-9]+).+?https:\/\/(.+?)\/(.+?)\/(.+?)\n", head)
    if match is None or len(match) == 0:
        print("Cannot check for updates: cannot parse FETCH_HEAD")
        return False

    match = match[0]

    local = match[0]
    host = match[1]
    owner = match[2]
    repo = match[3]

    res = curl(f"https://{host}/api/v1/repos/{owner}/{repo}/branches/") #this only works for gitea instances

    if res is None or len(res) == 0:
        print("Cannot check for updates: cannot fetch from remote")
        return False

    remote = res[0]["commit"]["id"]

    if remote != local:
        print(f"New version found: {local[:8]} => {remote[:8]}")
        return True

    return False

def update_voices():
    return gr.Dropdown.update(choices=sorted(os.listdir("./tortoise/voices")) + ["microphone"])

def export_exec_settings( share, listen, check_for_updates, low_vram, embed_output_metadata, latents_lean_and_mean, cond_latent_max_chunk_size, sample_batch_size, concurrency_count ):
    args.share = share
    args.listen = listen
    args.low_vram = low_vram
    args.check_for_updates = check_for_updates
    args.cond_latent_max_chunk_size = cond_latent_max_chunk_size
    args.sample_batch_size = sample_batch_size
    args.embed_output_metadata = embed_output_metadata
    args.latents_lean_and_mean = latents_lean_and_mean
    args.concurrency_count = concurrency_count

    settings = {
        'share': args.share,
        'listen': args.listen,
        'low-vram':args.low_vram,
        'check-for-updates':args.check_for_updates,
        'cond-latent-max-chunk-size': args.cond_latent_max_chunk_size,
        'sample-batch-size': args.sample_batch_size,
        'embed-output-metadata': args.embed_output_metadata,
        'latents-lean-and-mean': args.latents_lean_and_mean,
        'concurrency-count': args.concurrency_count,
    }

    with open(f'./config/exec.json', 'w', encoding="utf-8") as f:
        f.write(json.dumps(settings, indent='\t') )

def setup_args():
    default_arguments = {
        'share': False,
        'listen': None,
        'check-for-updates': False,
        'low-vram': False,
        'sample-batch-size': None,
        'embed-output-metadata': True,
        'latents-lean-and-mean': True,
        'cond-latent-max-chunk-size': 1000000,
        'concurrency-count': 3,
    }

    if os.path.isfile('./config/exec.json'):
        with open(f'./config/exec.json', 'r', encoding="utf-8") as f:
            overrides = json.load(f)
            for k in overrides:
                default_arguments[k] = overrides[k]

    parser = argparse.ArgumentParser()
    parser.add_argument("--share", action='store_true', default=default_arguments['share'], help="Lets Gradio return a public URL to use anywhere")
    parser.add_argument("--listen", default=default_arguments['listen'], help="Path for Gradio to listen on")
    parser.add_argument("--check-for-updates", action='store_true', default=default_arguments['check-for-updates'], help="Checks for update on startup")
    parser.add_argument("--low-vram", action='store_true', default=default_arguments['low-vram'], help="Disables some optimizations that increases VRAM usage")
    parser.add_argument("--no-embed-output-metadata", action='store_false', default=not default_arguments['embed-output-metadata'], help="Disables embedding output metadata into resulting WAV files for easily fetching its settings used with the web UI (data is stored in the lyrics metadata tag)")
    parser.add_argument("--latents-lean-and-mean", action='store_true', default=default_arguments['latents-lean-and-mean'], help="Exports the bare essentials for latents.")
    parser.add_argument("--cond-latent-max-chunk-size", default=default_arguments['cond-latent-max-chunk-size'], type=int, help="Sets an upper limit to audio chunk size when computing conditioning latents")
    parser.add_argument("--sample-batch-size", default=default_arguments['sample-batch-size'], type=int, help="Sets an upper limit to audio chunk size when computing conditioning latents")
    parser.add_argument("--concurrency-count", type=int, default=default_arguments['concurrency-count'], help="How many Gradio events to process at once")
    args = parser.parse_args()

    args.embed_output_metadata = not args.no_embed_output_metadata

    args.listen_host = None
    args.listen_port = None
    args.listen_path = None
    if args.listen is not None:
        match = re.findall(r"^(?:(.+?):(\d+))?(\/.+?)?$", args.listen)[0]

        args.listen_host = match[0] if match[0] != "" else "127.0.0.1"
        args.listen_port = match[1] if match[1] != "" else 8000
        args.listen_path = match[2] if match[2] != "" else "/"

    if args.listen_port is not None:
        args.listen_port = int(args.listen_port)
    
    return args

def setup_tortoise():
    print("Initializating TorToiSe...")
    tts = TextToSpeech(minor_optimizations=not args.low_vram)
    print("TorToiSe initialized, ready for generation.")
    return tts

def setup_gradio():
    if not args.share:
        def noop(function, return_value=None):
            def wrapped(*args, **kwargs):
                return return_value
            return wrapped
        gradio.utils.version_check = noop(gradio.utils.version_check)
        gradio.utils.initiated_analytics = noop(gradio.utils.initiated_analytics)
        gradio.utils.launch_analytics = noop(gradio.utils.launch_analytics)
        gradio.utils.integration_analytics = noop(gradio.utils.integration_analytics)
        gradio.utils.error_analytics = noop(gradio.utils.error_analytics)
        gradio.utils.log_feature_analytics = noop(gradio.utils.log_feature_analytics)
        #gradio.utils.get_local_ip_address = noop(gradio.utils.get_local_ip_address, 'localhost')

    with gr.Blocks() as webui:
        with gr.Tab("Generate"):
            with gr.Row():
                with gr.Column():
                    text = gr.Textbox(lines=4, label="Prompt")
                    delimiter = gr.Textbox(lines=1, label="Line Delimiter", placeholder="\\n")

                    emotion = gr.Radio(
                        ["Happy", "Sad", "Angry", "Disgusted", "Arrogant", "Custom"],
                        value="Custom",
                        label="Emotion",
                        type="value",
                        interactive=True
                    )
                    prompt = gr.Textbox(lines=1, label="Custom Emotion + Prompt (if selected)")
                    voice = gr.Dropdown(
                        sorted(os.listdir("./tortoise/voices")) + ["microphone"],
                        label="Voice",
                        type="value",
                    )
                    mic_audio = gr.Audio(
                        label="Microphone Source",
                        source="microphone",
                        type="filepath",
                    )
                    refresh_voices = gr.Button(value="Refresh Voice List")
                    refresh_voices.click(update_voices,
                        inputs=None,
                        outputs=voice
                    )
                    
                    prompt.change(fn=lambda value: gr.update(value="Custom"),
                        inputs=prompt,
                        outputs=emotion
                    )
                    mic_audio.change(fn=lambda value: gr.update(value="microphone"),
                        inputs=mic_audio,
                        outputs=voice
                    )
                with gr.Column():
                    candidates = gr.Slider(value=1, minimum=1, maximum=6, step=1, label="Candidates")
                    seed = gr.Number(value=0, precision=0, label="Seed")

                    preset = gr.Radio(
                        ["Ultra Fast", "Fast", "Standard", "High Quality"],
                        label="Preset",
                        type="value",
                    )
                    num_autoregressive_samples = gr.Slider(value=128, minimum=0, maximum=512, step=1, label="Samples")
                    diffusion_iterations = gr.Slider(value=128, minimum=0, maximum=512, step=1, label="Iterations")

                    temperature = gr.Slider(value=0.2, minimum=0, maximum=1, step=0.1, label="Temperature")
                    breathing_room = gr.Slider(value=8, minimum=1, maximum=32, step=1, label="Pause Size")
                    diffusion_sampler = gr.Radio(
                        ["P", "DDIM"], # + ["K_Euler_A", "DPM++2M"],
                        value="P",
                        label="Diffusion Samplers",
                        type="value",
                    )

                    preset.change(fn=update_presets,
                        inputs=preset,
                        outputs=[
                            num_autoregressive_samples,
                            diffusion_iterations,
                        ],
                    )
                with gr.Column():
                    selected_voice = gr.Audio(label="Source Sample")
                    output_audio = gr.Audio(label="Output")
                    usedSeed = gr.Textbox(label="Seed", placeholder="0", interactive=False) 
                    
                    submit = gr.Button(value="Generate")
                    #stop = gr.Button(value="Stop")
        with gr.Tab("Utilities"):
            with gr.Row():
                with gr.Column():
                    audio_in = gr.File(type="file", label="Audio Input", file_types=["audio"])
                    copy_button = gr.Button(value="Copy Settings")
                with gr.Column():
                    metadata_out = gr.JSON(label="Audio Metadata")
                    latents_out = gr.File(type="binary", label="Voice Latents")

                    audio_in.upload(
                        fn=read_generate_settings,
                        inputs=audio_in,
                        outputs=[
                            metadata_out,
                            latents_out
                        ]
                    )
        with gr.Tab("Settings"):
            with gr.Row():
                with gr.Column():
                    with gr.Box():
                        exec_arg_listen = gr.Textbox(label="Listen", value=args.listen, placeholder="127.0.0.1:7860/")
                        exec_arg_share = gr.Checkbox(label="Public Share Gradio", value=args.share)
                        exec_check_for_updates = gr.Checkbox(label="Check For Updates", value=args.check_for_updates)
                        exec_arg_low_vram = gr.Checkbox(label="Low VRAM", value=args.low_vram)
                        exec_arg_embed_output_metadata = gr.Checkbox(label="Embed Output Metadata", value=args.embed_output_metadata)
                        exec_arg_latents_lean_and_mean = gr.Checkbox(label="Slimmer Computed Latents", value=args.latents_lean_and_mean)
                        exec_arg_cond_latent_max_chunk_size = gr.Number(label="Voice Latents Max Chunk Size", precision=0, value=args.cond_latent_max_chunk_size)
                        exec_arg_sample_batch_size = gr.Number(label="Sample Batch Size", precision=0, value=args.sample_batch_size)
                        exec_arg_concurrency_count = gr.Number(label="Concurrency Count", precision=0, value=args.concurrency_count)


                    experimentals = gr.CheckboxGroup(["Half Precision", "Conditioning-Free"], value=["Conditioning-Free"], label="Experimental Flags")
                    cvvp_weight = gr.Slider(value=0, minimum=0, maximum=1, label="CVVP Weight")

                    check_updates_now = gr.Button(value="Check for Updates")

                    exec_inputs = [exec_arg_share, exec_arg_listen, exec_check_for_updates, exec_arg_low_vram, exec_arg_embed_output_metadata, exec_arg_latents_lean_and_mean, exec_arg_cond_latent_max_chunk_size, exec_arg_sample_batch_size, exec_arg_concurrency_count]

                    for i in exec_inputs:
                        i.change(
                            fn=export_exec_settings,
                            inputs=exec_inputs
                        )

                    check_updates_now.click(check_for_updates)

        input_settings = [
            text,
            delimiter,
            emotion,
            prompt,
            voice,
            mic_audio,
            seed,
            candidates,
            num_autoregressive_samples,
            diffusion_iterations,
            temperature,
            diffusion_sampler,
            breathing_room,
            cvvp_weight,
            experimentals,
        ]

        submit_event = submit.click(generate,
            inputs=input_settings,
            outputs=[selected_voice, output_audio, usedSeed],
        )

        copy_button.click(import_generate_settings,
            inputs=audio_in, # JSON elements cannt be used as inputs
            outputs=input_settings
        )

        if os.path.isfile('./config/generate.json'):
            webui.load(import_generate_settings, inputs=None, outputs=input_settings)
        
        if args.check_for_updates:
            webui.load(check_for_updates)

        #stop.click(fn=None, inputs=None, outputs=None, cancels=[submit_event])


    webui.queue(concurrency_count=args.concurrency_count)

    return webui

if __name__ == "__main__":
    args = setup_args()

    if args.listen_path is not None and args.listen_path != "/":
        import uvicorn
        uvicorn.run("app:app", host=args.listen_host, port=args.listen_port)
    else:
        webui = setup_gradio()
        webui.launch(share=args.share, prevent_thread_lock=True, server_name=args.listen_host, server_port=args.listen_port)
        tts = setup_tortoise()

        webui.block_thread()
elif __name__ == "app":
    import sys
    from fastapi import FastAPI

    sys.argv = [sys.argv[0]]

    app = FastAPI()
    args = setup_args()
    webui = setup_gradio()
    app = gr.mount_gradio_app(app, webui, path=args.listen_path)

    tts = setup_tortoise()
