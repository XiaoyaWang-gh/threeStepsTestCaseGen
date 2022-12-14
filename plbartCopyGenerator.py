""" PLBART model with copy mechanism """

from transformers import PLBartForConditionalGeneration, PLBartModel, AutoConfig
from transformers.modeling_outputs import Seq2SeqLMOutput, BaseModelOutput
from transformers.models.mbart.modeling_mbart import shift_tokens_right
from typing import Any, Dict
from torch import nn
import torch

# hf_checkpoint = "/data1/xiaoyawang/plbart_repo/hf_plbart_files"

class PLBartCopyGenerator(PLBartForConditionalGeneration):

    def __init__(self, config):
        super().__init__(config)

        self.model = PLBartModel(config)

        # Layers to compute the cross-attention
        self.attn_layer = nn.Linear(self.config.d_model, 1, bias=True)

        # Layers to compute p_gen
        self.pgen_context_layer = nn.Linear(self.config.d_model, 1, bias=True)
        self.pgen_decoder_output_layer = nn.Linear(self.config.d_model, 1, bias=True)
        self.pgen_decoder_prev_output_layer = nn.Linear(
            self.config.d_model, 1, bias=True
        )

        # Initialize weights and apply final processing
        self.init_weights()

    def _compute_cross_attn_prob(self, e, encoder_attentions=None):

        if self.config.centrality:
            centrality_scores = encoder_attentions[-1].mean(dim=1).mean(dim=1)
            centrality_scores = centrality_scores.unsqueeze(1).repeat_interleave(
                e.size(1), dim=1
            )

            if centrality_scores.shape[0] != e.shape[0]:
                centrality_scores = centrality_scores.repeat_interleave(
                    e.shape[0] // centrality_scores.shape[0], dim=0
                )

            # Add to e the centrality scores
            e += centrality_scores

        if self.config.tf_idf:
            # TODO
            pass

        return nn.Softmax(dim=-1)(e)

    @staticmethod
    def _shift_right_one_pad(x):

        shifted = x.roll(1)
        shifted[0] = 0
        return shifted

    def _compute_output_dist(
        self,
        encoder_outputs,
        decoder_outputs,
        encoder_input_ids,
    ):
        encoder_attentions = encoder_outputs.attentions
        encoder_outputs = encoder_outputs[0]
        decoder_outputs = decoder_outputs[0]
        source_len = encoder_outputs.shape[1]
        target_len = decoder_outputs.shape[1]
        batch_size = encoder_outputs.shape[0]

        proj_enc = encoder_outputs  # self.proj_enc_layer(encoder_outputs)
        proj_dec = decoder_outputs  # self.proj_dec_layer(decoder_outputs)

        # Sum the projected outputs and apply f_act to compute the cross-attention (Eq. 3)
        sum_projs = torch.nn.GELU()(
            (proj_dec[:, :, None, :] + proj_enc[:, None, :, :]).view(
                (batch_size, target_len, source_len, self.config.d_model)
            )
        )

        e = self.attn_layer(sum_projs).squeeze(-1)
        e[:, :, (encoder_input_ids == self.config.pad_token_id).nonzero()] = -100
        attns = self._compute_cross_attn_prob(e, encoder_attentions)

        context_vectors = torch.einsum("ijk, ikf -> ijf", attns, encoder_outputs)

        p_vocab_decoder = self.lm_head(decoder_outputs) + self.final_logits_bias
        p_vocab_context = self.lm_head(context_vectors) + self.final_logits_bias
        p_vocab = p_vocab_decoder + p_vocab_context
        p_vocab = nn.Softmax(dim=-1)(p_vocab)

        pgen_context = self.pgen_context_layer(context_vectors)
        pgen_decoder_output = self.pgen_decoder_output_layer(decoder_outputs)
        pgen_decoder_prev_output = self.pgen_decoder_prev_output_layer(
            PLBartCopyGenerator._shift_right_one_pad(decoder_outputs)
        )
        p_gen = nn.Sigmoid()(
            pgen_context + pgen_decoder_output + pgen_decoder_prev_output
        )
        
        p_copy = torch.zeros_like(p_vocab)

        ## Fix the size of the encoder_ids if beam search is being used.
        if encoder_input_ids.shape[0] != batch_size:
            encoder_input_ids = encoder_input_ids.repeat_interleave(
                batch_size // encoder_input_ids.shape[0], dim=0
            )

        p_copy = p_copy.scatter_add(
            -1,
            encoder_input_ids.repeat_interleave(attns.shape[1], dim=0).view(
                batch_size, target_len, -1
            ),
            attns,
        )

        final_dist = torch.log((1.0 - p_gen) * p_copy + p_gen * p_vocab)

        return final_dist

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        head_mask=None,
        decoder_head_mask=None,
        cross_attn_head_mask=None,
        encoder_outputs=None,
        past_key_values=None,
        inputs_embeds=None,
        decoder_inputs_embeds=None,
        labels=None,
        use_cache=None,
        output_attentions=True,
        output_hidden_states=None,
        return_dict=None,
    ):

        output_attentions = (
            output_attentions
            if output_attentions is not None
            else self.config.output_attentions
        )
        output_hidden_states = (
            output_hidden_states
            if output_hidden_states is not None
            else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = (
            return_dict if return_dict is not None else self.config.use_return_dict
        )

        if labels is not None:
            if decoder_input_ids is None:
                decoder_input_ids = shift_tokens_right(labels, self.config.pad_token_id)

        if decoder_input_ids is None and decoder_inputs_embeds is None:
            decoder_input_ids = shift_tokens_right(input_ids, self.config.pad_token_id)

        if encoder_outputs is None:
            encoder_outputs = self.model.get_encoder()(
                input_ids=input_ids,
                attention_mask=attention_mask,
                head_mask=head_mask,
                inputs_embeds=inputs_embeds,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )

        elif return_dict and not isinstance(encoder_outputs, BaseModelOutput):
            encoder_outputs = BaseModelOutput(
                last_hidden_state=encoder_outputs[0],
                hidden_states=encoder_outputs[1] if len(encoder_outputs) > 1 else None,
                attentions=encoder_outputs[2] if len(encoder_outputs) > 2 else None,
            )

        decoder_outputs = self.model.get_decoder()(
            input_ids=decoder_input_ids,
            attention_mask=decoder_attention_mask,
            encoder_hidden_states=encoder_outputs[0],
            encoder_attention_mask=attention_mask,
            head_mask=decoder_head_mask,
            cross_attn_head_mask=cross_attn_head_mask,
            past_key_values=past_key_values,
            inputs_embeds=decoder_inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        logits = self._compute_output_dist(
            encoder_outputs,
            decoder_outputs,
            input_ids,
        )

        masked_lm_loss = None
        if labels is not None:
            loss_fct = nn.NLLLoss()
            masked_lm_loss = loss_fct(
                logits.view(-1, self.config.vocab_size), labels.view(-1)
            )

        if not return_dict:
            output = (logits,) + decoder_outputs[1:]
            return (
                ((masked_lm_loss,) + output) if masked_lm_loss is not None else output
            )

        return Seq2SeqLMOutput(
            loss=masked_lm_loss,
            logits=logits,
            past_key_values=decoder_outputs.past_key_values,
            decoder_hidden_states=decoder_outputs.hidden_states,
            decoder_attentions=decoder_outputs.attentions,
            cross_attentions=decoder_outputs.cross_attentions,
            encoder_last_hidden_state=encoder_outputs.last_hidden_state,
            encoder_hidden_states=encoder_outputs.hidden_states,
            encoder_attentions=encoder_outputs.attentions,
        )

    def _prepare_encoder_decoder_kwargs_for_generation(
        self, input_ids: torch.LongTensor, model_kwargs
    ) -> Dict[str, Any]:
        if "encoder_outputs" not in model_kwargs:
            # retrieve encoder hidden states
            encoder = self.get_encoder()
            encoder_kwargs = {
                argument: value
                for argument, value in model_kwargs.items()
                if not (
                    argument.startswith("decoder_") or argument.startswith("cross_attn")
                )
            }
            encoder_kwargs["output_attentions"] = True
            # model_kwargs["encoder_outputs"]: ModelOutput = encoder(
            model_kwargs["encoder_outputs"] = encoder(    
                input_ids, return_dict=True, **encoder_kwargs
            )
        model_kwargs["encoder_input_ids"] = input_ids
        return model_kwargs

    def prepare_inputs_for_generation(
        self,
        decoder_input_ids,
        past=None,
        attention_mask=None,
        head_mask=None,
        decoder_head_mask=None,
        cross_attn_head_mask=None,
        use_cache=None,
        encoder_outputs=None,
        **kwargs
    ):
        # cut decoder_input_ids if past is used
        if past is not None:
            decoder_input_ids = decoder_input_ids[:, -1:]

        return {
            "input_ids": kwargs[
                "encoder_input_ids"
            ],  # input_ids are needed for the copy mechanism even in inference.
            "encoder_outputs": encoder_outputs,
            "past_key_values": past,
            "decoder_input_ids": decoder_input_ids,
            "attention_mask": attention_mask,
            "head_mask": head_mask,
            "decoder_head_mask": decoder_head_mask,
            "cross_attn_head_mask": cross_attn_head_mask,
            "use_cache": use_cache,  # change this to avoid caching (presumably for debugging)
        }
