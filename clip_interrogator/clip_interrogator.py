import hashlib
import inspect
import math
import numpy as np
import open_clip
import os
import requests
import time
import torch

from dataclasses import dataclass
from blip.models.blip import blip_decoder, BLIP_Decoder
from PIL import Image
from torchvision import transforms
from torchvision.transforms.functional import InterpolationMode
from tqdm import tqdm
from typing import List, Optional

from safetensors.numpy import load_file, save_file

BLIP_MODELS = {
    'base': 'https://storage.googleapis.com/sfr-vision-language-research/BLIP/models/model_base_caption_capfilt_large.pth',
    'large': 'https://storage.googleapis.com/sfr-vision-language-research/BLIP/models/model_large_caption.pth'
}

CACHE_URL_BASE = 'https://huggingface.co/pharma/ci-preprocess/resolve/main/'


@dataclass 
class Config:
    # models can optionally be passed in directly
    blip_model: BLIP_Decoder = None
    clip_model = None
    clip_preprocess = None

    # blip settings
    blip_image_eval_size: int = 384
    blip_max_length: int = 32
    blip_model_type: str = 'large' # choose between 'base' or 'large'
    blip_num_beams: int = 8
    blip_offload: bool = False

    # clip settings
    clip_model_name: str = 'ViT-L-14/openai'
    clip_model_path: str = None
    clip_offload: bool = False

    # interrogator settings
    cache_path: str = 'cache'   # path to store cached text embeddings
    download_cache: bool = True # when true, cached embeds are downloaded from huggingface
    chunk_size: int = 2048      # batch size for CLIP, use smaller for lower VRAM
    data_path: str = os.path.join(os.path.dirname(__file__), 'data')
    device: str = ("mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu")
    flavor_intermediate_count: int = 2048
    quiet: bool = False # when quiet progress bars are not shown

    def apply_low_vram_defaults(self):
        self.blip_model_type = 'base'
        self.blip_offload = True
        self.clip_offload = True
        self.chunk_size = 1024
        self.flavor_intermediate_count = 1024

class Interrogator():
    def __init__(self, config: Config):
        self.config = config
        self.device = config.device
        self.blip_offloaded = True
        self.clip_offloaded = True

        if config.blip_model is None:
            if not config.quiet:
                print("Loading BLIP model...")
            blip_path = os.path.dirname(inspect.getfile(blip_decoder))
            configs_path = os.path.join(os.path.dirname(blip_path), 'configs')
            med_config = os.path.join(configs_path, 'med_config.json')
            blip_model = blip_decoder(
                pretrained=BLIP_MODELS[config.blip_model_type],
                image_size=config.blip_image_eval_size, 
                vit=config.blip_model_type, 
                med_config=med_config
            )
            blip_model.eval()
            if not self.config.blip_offload:
                blip_model = blip_model.to(config.device)
            self.blip_model = blip_model
        else:
            self.blip_model = config.blip_model

        self.load_clip_model()

    def load_clip_model(self):
        start_time = time.time()
        config = self.config

        clip_model_name, clip_model_pretrained_name = config.clip_model_name.split('/', 2)

        if config.clip_model is None:
            if not config.quiet:
                print("Loading CLIP model...")

            self.clip_model, _, self.clip_preprocess = open_clip.create_model_and_transforms(
                clip_model_name, 
                pretrained=clip_model_pretrained_name, 
                precision='fp16' if config.device == 'cuda' else 'fp32',
                device=config.device,
                jit=False,
                cache_dir=config.clip_model_path
            )
            self.clip_model.eval()
        else:
            self.clip_model = config.clip_model
            self.clip_preprocess = config.clip_preprocess
        self.tokenize = open_clip.get_tokenizer(clip_model_name)

        sites = ['Artstation', 'behance', 'cg society', 'cgsociety', 'deviantart', 'dribble', 
                 'flickr', 'instagram', 'pexels', 'pinterest', 'pixabay', 'pixiv', 'polycount', 
                 'reddit', 'shutterstock', 'tumblr', 'unsplash', 'zbrush central']
        trending_list = [site for site in sites]
        trending_list.extend(["trending on "+site for site in sites])
        trending_list.extend(["featured on "+site for site in sites])
        trending_list.extend([site+" contest winner" for site in sites])

        raw_artists = _load_list(config.data_path, 'artists.txt')
        artists = [f"by {a}" for a in raw_artists]
        artists.extend([f"inspired by {a}" for a in raw_artists])

        self._prepare_clip()
        self.artists = LabelTable(artists, "artists", self.clip_model, self.tokenize, config)
        self.flavors = LabelTable(_load_list(config.data_path, 'flavors.txt'), "flavors", self.clip_model, self.tokenize, config)
        self.colors = LabelTable(_load_list(config.data_path, 'colors.txt'), "colors", self.clip_model, self.tokenize, config)
        self.nouns = LabelTable(_load_list(config.data_path, 'nouns.txt'), "nouns", self.clip_model, self.tokenize, config)
        self.mediums = LabelTable(_load_list(config.data_path, 'mediums.txt'), "mediums", self.clip_model, self.tokenize, config)
        self.movements = LabelTable(_load_list(config.data_path, 'movements.txt'), "movements", self.clip_model, self.tokenize, config)
        self.trendings = LabelTable(trending_list, "trendings", self.clip_model, self.tokenize, config)
        self.negative = LabelTable(_load_list(config.data_path, 'negative.txt'), "negative", self.clip_model, self.tokenize, config)

        end_time = time.time()
        if not config.quiet:
            print(f"Loaded CLIP model and data in {end_time-start_time:.2f} seconds.")

    def chain(
        self, 
        image_features: torch.Tensor, 
        phrases: List[str], 
        best_prompt: str="", 
        best_sim: float=0, 
        min_count: int=8,
        max_count: int=32, 
        desc="Chaining", 
        reverse: bool=False
    ) -> str:
        self._prepare_clip()

        phrases = set(phrases)
        if not best_prompt:
            best_prompt = self.rank_top(image_features, [f for f in phrases], reverse=reverse)
            best_sim = self.similarity(image_features, best_prompt)
            phrases.remove(best_prompt)
        curr_prompt, curr_sim = best_prompt, best_sim
        
        def check(addition: str, idx: int) -> bool:
            nonlocal best_prompt, best_sim, curr_prompt, curr_sim
            prompt = curr_prompt + ", " + addition
            sim = self.similarity(image_features, prompt)
            if reverse:
                sim = -sim
            
            if sim > best_sim:
                best_prompt, best_sim = prompt, sim
            if sim > curr_sim or idx < min_count:
                curr_prompt, curr_sim = prompt, sim
                return True
            return False

        for idx in tqdm(range(max_count), desc=desc, disable=self.config.quiet):
            best = self.rank_top(image_features, [f"{curr_prompt}, {f}" for f in phrases], reverse=reverse)
            flave = best[len(curr_prompt)+2:]
            if not check(flave, idx):
                break
            if _prompt_at_max_len(curr_prompt, self.tokenize):
                break
            phrases.remove(flave)

        return best_prompt

    def generate_caption(self, pil_image: Image) -> str:
        self._prepare_blip()

        size = self.config.blip_image_eval_size
        gpu_image = transforms.Compose([
            transforms.Resize((size, size), interpolation=InterpolationMode.BICUBIC),
            transforms.ToTensor(),
            transforms.Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711))
        ])(pil_image).unsqueeze(0).to(self.device)

        with torch.no_grad():
            caption = self.blip_model.generate(
                gpu_image, 
                sample=False, 
                num_beams=self.config.blip_num_beams, 
                max_length=self.config.blip_max_length, 
                min_length=5
            )

        return caption[0]

    def image_to_features(self, image: Image) -> torch.Tensor:
        self._prepare_clip()
        images = self.clip_preprocess(image).unsqueeze(0).to(self.device)
        with torch.no_grad(), torch.cuda.amp.autocast():
            image_features = self.clip_model.encode_image(images)
            image_features /= image_features.norm(dim=-1, keepdim=True)
        return image_features

    def interrogate_classic(self, image: Image, max_flavors: int=3, caption: Optional[str]=None) -> str:
        """Classic mode creates a prompt in a standard format first describing the image, 
        then listing the artist, color, trending, movement, and flavor text modifiers."""
        caption = caption or self.generate_caption(image)
        image_features = self.image_to_features(image)

        medium = self.mediums.rank(image_features, 1)[0]
        artist = self.artists.rank(image_features, 1)[0]
        # color = self.colors.rank(image_features, 1)[0]
        trending = self.trendings.rank(image_features, 1)[0]
        movement = self.movements.rank(image_features, 1)[0]
        flaves = ", ".join(self.flavors.rank(image_features, max_flavors))
        color = ", ".join(self.colors.rank(image_features, 10))
        noun = ", ".join(self.nouns.rank(image_features, 10))

        if caption.startswith(medium):
            prompt = f"{caption} {artist}, {color}, {noun}, {trending}, {movement}, {flaves}"
        else:
            prompt = f"{caption}, {medium} {artist}, {color}, {noun}, {trending}, {movement}, {flaves}"

        return _truncate_to_fit(prompt, self.tokenize)

    def interrogate_fast(self, image: Image, max_flavors: int=32, caption: Optional[str]=None) -> str:
        """Fast mode simply adds the top ranked terms after a caption. It generally results in 
        better similarity between generated prompt and image than classic mode, but the prompts
        are less readable."""
        caption = caption or self.generate_caption(image)
        image_features = self.image_to_features(image)
        merged = _merge_tables([self.artists, self.colors, self.nouns, self.flavors, self.mediums, self.movements, self.trendings], self.config)
        tops = merged.rank(image_features, max_flavors)
        return _truncate_to_fit(caption + ", " + ", ".join(tops), self.tokenize)

    def interrogate_negative(self, image: Image, max_flavors: int = 32) -> str:
        """Negative mode chains together the most dissimilar terms to the image. It can be used
        to help build a negative prompt to pair with the regular positive prompt and often 
        improve the results of generated images particularly with Stable Diffusion 2."""
        image_features = self.image_to_features(image)
        flaves = self.flavors.rank(image_features, self.config.flavor_intermediate_count, reverse=True)
        flaves = flaves + self.negative.labels
        return self.chain(image_features, flaves, max_count=max_flavors, reverse=True, desc="Negative chain")

    def interrogate(self, image: Image, min_flavors: int=8, max_flavors: int=32, caption: Optional[str]=None) -> str:
        caption = caption or self.generate_caption(image)
        print(f"BLIP caption: {caption}")
        image_features = self.image_to_features(image)

        # merged = _merge_tables([self.artists, self.colors, self.flavors, self.mediums, self.movements, self.trendings], self.config)
        merged = _merge_tables([self.artists, self.colors, self.nouns, self.movements], self.config)
        flaves = merged.rank(image_features, self.config.flavor_intermediate_count)
        best_prompt, best_sim = caption, self.similarity(image_features, caption)
        best_prompt = self.chain(image_features, flaves, best_prompt, best_sim, min_count=min_flavors, max_count=max_flavors, desc="Flavor chain")

        fast_prompt = self.interrogate_fast(image, max_flavors, caption=caption)
        classic_prompt = self.interrogate_classic(image, max_flavors, caption=caption)
        candidates = [caption, classic_prompt, fast_prompt, best_prompt]
        return candidates[np.argmax(self.similarities(image_features, candidates))]

    def rank_top(self, image_features: torch.Tensor, text_array: List[str], reverse: bool=False) -> str:
        self._prepare_clip()
        text_tokens = self.tokenize([text for text in text_array]).to(self.device)
        with torch.no_grad(), torch.cuda.amp.autocast():
            text_features = self.clip_model.encode_text(text_tokens)
            text_features /= text_features.norm(dim=-1, keepdim=True)
            similarity = text_features @ image_features.T
            if reverse:
                similarity = -similarity
        return text_array[similarity.argmax().item()]

    def similarity(self, image_features: torch.Tensor, text: str) -> float:
        self._prepare_clip()
        text_tokens = self.tokenize([text]).to(self.device)
        with torch.no_grad(), torch.cuda.amp.autocast():
            text_features = self.clip_model.encode_text(text_tokens)
            text_features /= text_features.norm(dim=-1, keepdim=True)
            similarity = text_features @ image_features.T
        return similarity[0][0].item()

    def similarities(self, image_features: torch.Tensor, text_array: List[str]) -> List[float]:
        self._prepare_clip()
        text_tokens = self.tokenize([text for text in text_array]).to(self.device)
        with torch.no_grad(), torch.cuda.amp.autocast():
            text_features = self.clip_model.encode_text(text_tokens)
            text_features /= text_features.norm(dim=-1, keepdim=True)
            similarity = text_features @ image_features.T
        return similarity.T[0].tolist()

    def _prepare_blip(self):
        if self.config.clip_offload and not self.clip_offloaded:
            self.clip_model = self.clip_model.to('cpu')
            self.clip_offloaded = True
        if self.blip_offloaded:
            self.blip_model = self.blip_model.to(self.device)
            self.blip_offloaded = False

    def _prepare_clip(self):
        if self.config.blip_offload and not self.blip_offloaded:
            self.blip_model = self.blip_model.to('cpu')
            self.blip_offloaded = True
        if self.clip_offloaded:
            self.clip_model = self.clip_model.to(self.device)
            self.clip_offloaded = False


class LabelTable():
    def __init__(self, labels:List[str], desc:str, clip_model, tokenize, config: Config):
        self.chunk_size = config.chunk_size
        self.config = config
        self.device = config.device
        self.embeds = []
        self.labels = labels
        self.tokenize = tokenize

        hash = hashlib.sha256(",".join(labels).encode()).hexdigest()
        sanitized_name = self.config.clip_model_name.replace('/', '_').replace('@', '_')
        self._load_cached(desc, hash, sanitized_name)

        if len(self.labels) != len(self.embeds):
            self.embeds = []
            chunks = np.array_split(self.labels, max(1, len(self.labels)/config.chunk_size))
            for chunk in tqdm(chunks, desc=f"Preprocessing {desc}" if desc else None, disable=self.config.quiet):
                text_tokens = self.tokenize(chunk).to(self.device)
                with torch.no_grad(), torch.cuda.amp.autocast():
                    text_features = clip_model.encode_text(text_tokens)
                    text_features /= text_features.norm(dim=-1, keepdim=True)
                    text_features = text_features.half().cpu().numpy()
                for i in range(text_features.shape[0]):
                    self.embeds.append(text_features[i])

            if desc and self.config.cache_path:
                os.makedirs(self.config.cache_path, exist_ok=True)
                cache_filepath = os.path.join(self.config.cache_path, f"{sanitized_name}_{desc}.safetensors")
                tensors = {
                    "embeds": np.stack(self.embeds),
                    "hash": np.array([ord(c) for c in hash], dtype=np.int8)
                }
                save_file(tensors, cache_filepath)

        if self.device == 'cpu' or self.device == torch.device('cpu'):
            self.embeds = [e.astype(np.float32) for e in self.embeds]

    def _load_cached(self, desc:str, hash:str, sanitized_name:str) -> bool:
        if self.config.cache_path is None or desc is None:
            return False

        cached_safetensors = os.path.join(self.config.cache_path, f"{sanitized_name}_{desc}.safetensors")

        if self.config.download_cache and not os.path.exists(cached_safetensors):
            download_url = CACHE_URL_BASE + f"{sanitized_name}_{desc}.safetensors"
            try:
                os.makedirs(self.config.cache_path, exist_ok=True)
                _download_file(download_url, cached_safetensors, quiet=self.config.quiet)
            except Exception as e:
                print(f"Failed to download {download_url}")
                print(e)
                return False                

        if os.path.exists(cached_safetensors):
            try:
                tensors = load_file(cached_safetensors)
            except Exception as e:
                print(f"Failed to load {cached_safetensors}")
                print(e)
                return False
            if 'hash' in tensors and 'embeds' in tensors:
                if np.array_equal(tensors['hash'], np.array([ord(c) for c in hash], dtype=np.int8)):
                    self.embeds = tensors['embeds']
                    if len(self.embeds.shape) == 2:
                        self.embeds = [self.embeds[i] for i in range(self.embeds.shape[0])]
                    return True

        return False
    
    def _rank(self, image_features: torch.Tensor, text_embeds: torch.Tensor, top_count: int=1, reverse: bool=False) -> str:
        top_count = min(top_count, len(text_embeds))
        text_embeds = torch.stack([torch.from_numpy(t) for t in text_embeds]).to(self.device)
        with torch.cuda.amp.autocast():
            similarity = image_features @ text_embeds.T
            if reverse:
                similarity = -similarity
        _, top_labels = similarity.float().cpu().topk(top_count, dim=-1)
        return [top_labels[0][i].numpy() for i in range(top_count)]

    def rank(self, image_features: torch.Tensor, top_count: int=1, reverse: bool=False) -> List[str]:
        if len(self.labels) <= self.chunk_size:
            tops = self._rank(image_features, self.embeds, top_count=top_count, reverse=reverse)
            return [self.labels[i] for i in tops]

        num_chunks = int(math.ceil(len(self.labels)/self.chunk_size))
        keep_per_chunk = int(self.chunk_size / num_chunks)

        top_labels, top_embeds = [], []
        for chunk_idx in tqdm(range(num_chunks), disable=self.config.quiet):
            start = chunk_idx*self.chunk_size
            stop = min(start+self.chunk_size, len(self.embeds))
            tops = self._rank(image_features, self.embeds[start:stop], top_count=keep_per_chunk, reverse=reverse)
            top_labels.extend([self.labels[start+i] for i in tops])
            top_embeds.extend([self.embeds[start+i] for i in tops])

        tops = self._rank(image_features, top_embeds, top_count=top_count)
        return [top_labels[i] for i in tops]


def _download_file(url: str, filepath: str, chunk_size: int = 4*1024*1024, quiet: bool = False):
    r = requests.get(url, stream=True)
    if r.status_code != 200:
        return

    file_size = int(r.headers.get("Content-Length", 0))
    filename = url.split("/")[-1]
    progress = tqdm(total=file_size, unit="B", unit_scale=True, desc=filename, disable=quiet)
    with open(filepath, "wb") as f:
        for chunk in r.iter_content(chunk_size=chunk_size):
            if chunk:
                f.write(chunk)
                progress.update(len(chunk))
    progress.close()

def _load_list(data_path: str, filename: str) -> List[str]:
    with open(os.path.join(data_path, filename), 'r', encoding='utf-8', errors='replace') as f:
        items = [line.strip() for line in f.readlines()]
    return items

def _merge_tables(tables: List[LabelTable], config: Config) -> LabelTable:
    m = LabelTable([], None, None, None, config)
    for table in tables:
        m.labels.extend(table.labels)
        m.embeds.extend(table.embeds)
    return m

def _prompt_at_max_len(text: str, tokenize) -> bool:
    tokens = tokenize([text])
    return tokens[0][-1] != 0

def _truncate_to_fit(text: str, tokenize) -> str:
    parts = text.split(', ')
    new_text = parts[0]
    for part in parts[1:]:
        if _prompt_at_max_len(new_text + part, tokenize):
            break
        new_text += ', ' + part
    return new_text
